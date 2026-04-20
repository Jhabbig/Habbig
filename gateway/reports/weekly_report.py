"""Weekly Intelligence Report — data collection, Claude narrative, PDF generation.

Pipeline: collect data → Claude writes narrative → render HTML → convert to PDF
→ store on disk → email with attachment → record in DB.

Follows the gateway's patterns:
  - Lazy Claude: stubs if API key missing, never crashes
  - Email via enqueue_email (job queue)
  - PDF via WeasyPrint (HTML → PDF, CSS-styled)
  - One report per user per week (UNIQUE constraint in DB)
"""

from __future__ import annotations

import datetime as _dt
import html as _html
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import db

log = logging.getLogger("gateway.reports")

# Base directory for generated PDFs. Each user gets a subdirectory.
REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", "")) or (Path(__file__).parent.parent / "generated_reports")

# Claude model for narrative generation
REPORT_MODEL = os.environ.get("REPORT_MODEL", "claude-sonnet-4-5-20250929")


# ── Data collection ──────────────────────────────────────────────────────────

def get_week_bounds(reference: Optional[_dt.datetime] = None) -> tuple[int, int]:
    """Return (week_start, week_end) as unix timestamps for the Monday-to-Sunday
    week containing `reference` (default: last completed week).

    If called on a Monday, returns the previous week (Mon-Sun), not the
    current week that just started.
    """
    if reference is None:
        reference = _dt.datetime.now(_dt.timezone.utc)
    # Walk back to the most recent Monday 00:00 UTC
    days_since_monday = reference.weekday()  # 0=Mon
    this_monday = reference.replace(hour=0, minute=0, second=0, microsecond=0) - _dt.timedelta(days=days_since_monday)
    # If today IS Monday, we report on the PREVIOUS week
    if days_since_monday == 0 and reference.hour < 12:
        this_monday -= _dt.timedelta(days=7)
    week_start = int(this_monday.timestamp())
    week_end = week_start + 7 * 86400
    return week_start, week_end


def collect_report_data(user_id: int, week_start: int, week_end: int) -> dict:
    """Gather all data needed for a single user's weekly report.

    Combines the global week data from db.get_report_data_for_week with
    per-user context (topics, followed sources, saved predictions).
    """
    # Global data — shared across all users for this week
    global_data = db.get_report_data_for_week(week_start, week_end)

    # Per-user data
    user = db.get_user_by_id(user_id)
    display_name = (user["username"] if user else "Subscriber") if user else "Subscriber"

    # User's Signal Search topics (Pro only)
    topics = []
    try:
        with db.conn() as c:
            rows = c.execute(
                "SELECT ut.*, uta.summary, uta.signal_direction, uta.confidence "
                "FROM user_topics ut "
                "LEFT JOIN user_topic_analyses uta ON uta.user_topic_id = ut.id "
                "WHERE ut.user_id = ? AND ut.is_active = 1 "
                "ORDER BY ut.created_at",
                (user_id,),
            ).fetchall()
            topics = [dict(r) for r in rows]
    except Exception as e:
        log.warning("Failed to fetch topics for user %d: %s", user_id, e)

    # User's followed sources — filter source_perf to highlight ones they follow
    followed = set()
    try:
        with db.conn() as c:
            rows = c.execute(
                "SELECT source_handle FROM followed_sources WHERE user_id = ?",
                (user_id,),
            ).fetchall()
            followed = {r["source_handle"] for r in rows}
    except Exception:
        pass

    return {
        **global_data,
        "user_id": user_id,
        "display_name": display_name,
        "user_topics": topics,
        "followed_sources": followed,
        "week_start": week_start,
        "week_end": week_end,
    }


# ── Claude narrative generation ──────────────────────────────────────────────

NARRATIVE_SYSTEM_PROMPT = """You are a financial intelligence analyst writing the weekly report for narve.ai, a prediction market intelligence platform.

Write in a professional, direct tone — like a Bloomberg terminal note, not a blog post. Use specific numbers. No filler. Every sentence should contain data or insight.

You will receive structured data about the past week's predictions, source performance, and market activity. Write these narrative sections:

1. EXECUTIVE SUMMARY (2-3 sentences): total predictions, high-credibility accuracy rate, standout signal of the week with specific numbers
2. BEST BETS ANALYSIS (1-2 sentences per resolved bet): what happened, why the signal was right/wrong, edge captured
3. NOTABLE SOURCE BEHAVIOUR (1-2 sentences): call out any interesting pattern — a source's streak, a contrarian call, an emerging analyst
4. MARKETS TO WATCH (1 sentence per market): what's coming next week and what the signals say

Return your response as JSON with keys: "executive_summary", "best_bets_analysis" (array of strings, one per bet), "notable_source", "markets_to_watch" (array of strings, one per market).
"""


async def generate_narratives(data: dict) -> dict:
    """Call Claude to write the report's narrative sections.

    Returns a dict of narrative strings keyed by section name.
    Falls back to placeholder text if Claude is unavailable — the report
    still generates, just without the AI-written analysis.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        log.warning("weekly report: ANTHROPIC_API_KEY not set, using placeholder narratives")
        return _placeholder_narratives(data)

    try:
        import anthropic
    except ImportError:
        log.warning("weekly report: anthropic SDK not installed, using placeholder narratives")
        return _placeholder_narratives(data)

    # Build context from the collected data
    resolved = data.get("resolved_predictions", [])
    sources = data.get("top_sources", [])
    context = {
        "week_start": _dt.datetime.fromtimestamp(data["week_start"], tz=_dt.timezone.utc).strftime("%B %d, %Y"),
        "week_end": _dt.datetime.fromtimestamp(data["week_end"], tz=_dt.timezone.utc).strftime("%B %d, %Y"),
        "total_predictions": data["total_predictions"],
        "total_markets": data["total_markets"],
        "high_cred_correct": data["high_cred_correct"],
        "high_cred_total": data["high_cred_total"],
        "high_cred_accuracy": (
            f"{data['high_cred_correct'] / data['high_cred_total'] * 100:.1f}%"
            if data["high_cred_total"] > 0 else "N/A"
        ),
        "resolved_predictions": [
            {
                "market_id": p.get("market_id", ""),
                "direction": p.get("direction", ""),
                "resolved_correct": bool(p.get("resolved_correct")),
                "source_handle": p.get("source_handle", ""),
                "credibility": p.get("global_credibility"),
                "content": (p.get("content") or "")[:200],
            }
            for p in resolved[:20]
        ],
        "top_sources": [
            {
                "handle": s.get("source_handle", ""),
                "correct": s.get("correct", 0),
                "total": s.get("total", 0),
                "credibility": s.get("global_credibility"),
            }
            for s in sources[:10]
        ],
    }

    user_message = f"Generate the weekly intelligence report narrative sections for this data:\n\n{json.dumps(context, indent=2)}"

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=REPORT_MODEL,
            max_tokens=2048,
            system=NARRATIVE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        text = response.content[0].text
        # Parse as JSON
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Claude sometimes wraps JSON in ```json blocks
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
                return json.loads(text)
            log.warning("weekly report: Claude returned non-JSON: %s", text[:200])
            return _placeholder_narratives(data)
    except Exception as exc:
        log.exception("weekly report: Claude call failed: %s", exc)
        return _placeholder_narratives(data)


def _placeholder_narratives(data: dict) -> dict:
    """Fallback narratives when Claude is unavailable."""
    total = data["total_predictions"]
    markets = data["total_markets"]
    hc_correct = data["high_cred_correct"]
    hc_total = data["high_cred_total"]
    hc_pct = f"{hc_correct / hc_total * 100:.1f}%" if hc_total > 0 else "N/A"

    return {
        "executive_summary": (
            f"This week narve.ai tracked {total} predictions across {markets} markets. "
            f"High-credibility signals (score >= 0.7) achieved {hc_pct} accuracy ({hc_correct}/{hc_total})."
        ),
        "best_bets_analysis": [],
        "notable_source": "",
        "markets_to_watch": [],
    }


# ── PDF rendering ────────────────────────────────────────────────────────────

REPORT_CSS = """
@page { size: A4; margin: 40px 50px; }
body {
    font-family: Georgia, 'Times New Roman', serif;
    color: #0d0d0d; font-size: 11pt; line-height: 1.6;
    max-width: 100%; margin: 0;
}
.header { border-bottom: 2px solid #0d0d0d; padding-bottom: 12px; margin-bottom: 24px; }
.title { font-size: 22pt; font-weight: normal; letter-spacing: -0.02em; margin: 0; }
.subtitle { font-size: 10pt; color: #666; margin-top: 4px; }
h2 {
    font-size: 10pt; font-weight: bold; text-transform: uppercase;
    letter-spacing: 0.1em; margin-top: 28px; margin-bottom: 12px;
    border-bottom: 1px solid #e0e0e0; padding-bottom: 6px;
}
.stat-row { display: flex; gap: 40px; margin: 16px 0 24px; }
.stat { text-align: center; }
.stat-value { font-size: 24pt; font-weight: bold; display: block; }
.stat-label { font-size: 8pt; color: #666; text-transform: uppercase; letter-spacing: 0.08em; }
.bet { margin: 8px 0; padding: 10px 14px; border-left: 3px solid #ccc; }
.bet.correct { border-left-color: #0d0d0d; }
.bet.incorrect { border-left-color: #ddd; color: #888; }
.bet-title { font-weight: bold; font-size: 10pt; }
.bet-detail { font-size: 9pt; color: #444; margin-top: 2px; }
.source-row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #f0f0f0; font-size: 10pt; }
.source-handle { font-family: 'Courier New', monospace; font-weight: bold; }
.source-stat { color: #666; }
.topic-card { border: 1px solid #e0e0e0; border-radius: 4px; padding: 12px; margin: 8px 0; font-size: 10pt; }
.topic-name { font-weight: bold; margin-bottom: 4px; }
.topic-summary { color: #444; font-size: 9pt; }
.divider { border: none; border-top: 1px solid #e0e0e0; margin: 20px 0; }
.footer { font-size: 8pt; color: #aaa; text-align: center; margin-top: 40px; border-top: 1px solid #e0e0e0; padding-top: 12px; }
.narrative { font-style: italic; color: #333; margin: 8px 0 16px; font-size: 10pt; line-height: 1.5; }
"""


def render_report_html(data: dict, narratives: dict) -> str:
    """Render the full report as an HTML string suitable for WeasyPrint."""
    ws = _dt.datetime.fromtimestamp(data["week_start"], tz=_dt.timezone.utc)
    we = _dt.datetime.fromtimestamp(data["week_end"], tz=_dt.timezone.utc) - _dt.timedelta(days=1)

    total = data["total_predictions"]
    markets = data["total_markets"]
    hc_correct = data["high_cred_correct"]
    hc_total = data["high_cred_total"]
    hc_pct = f"{hc_correct / hc_total * 100:.1f}%" if hc_total > 0 else "N/A"

    resolved = data.get("resolved_predictions", [])
    sources = data.get("top_sources", [])
    topics = data.get("user_topics", [])

    # Resolved predictions — first 10 as "best bets"
    bets_html = ""
    correct_count = 0
    bet_analyses = narratives.get("best_bets_analysis", [])
    for i, p in enumerate(resolved[:10]):
        is_correct = bool(p.get("resolved_correct"))
        if is_correct:
            correct_count += 1
        cls = "correct" if is_correct else "incorrect"
        icon = "&#10003;" if is_correct else "&#10007;"
        analysis = bet_analyses[i] if i < len(bet_analyses) else ""
        content_preview = _html.escape((p.get("content") or "")[:120])
        source = _html.escape(p.get("source_handle") or "unknown")
        cred = p.get("global_credibility")
        cred_str = f" (credibility: {cred:.2f})" if cred else ""

        bets_html += f"""
        <div class="bet {cls}">
            <div class="bet-title">{icon} {content_preview}</div>
            <div class="bet-detail">Source: @{source}{cred_str} &middot; Direction: {p.get('direction', '?')}</div>
            {"<div class='narrative'>" + _html.escape(analysis) + "</div>" if analysis else ""}
        </div>
        """

    bet_total = min(len(resolved), 10)
    roi = ((correct_count / bet_total * 100) - 50) * 0.4 if bet_total > 0 else 0.0

    # Source performance table
    sources_html = ""
    for s in sources[:8]:
        handle = _html.escape(s.get("source_handle", ""))
        correct = s.get("correct", 0)
        total_s = s.get("total", 0)
        cred = s.get("global_credibility")
        cred_str = f"{cred:.2f}" if cred else "—"
        pct = f"{correct}/{total_s}" if total_s else "—"
        sources_html += f"""
        <div class="source-row">
            <span class="source-handle">@{handle}</span>
            <span class="source-stat">{pct} correct &middot; Credibility: {cred_str}</span>
        </div>
        """

    # User topics
    topics_html = ""
    for t in topics[:5]:
        name = _html.escape(t.get("name") or "Unnamed topic")
        summary = _html.escape((t.get("summary") or "No analysis yet.")[:200])
        direction = t.get("signal_direction", "unclear")
        confidence = t.get("confidence", "—")
        topics_html += f"""
        <div class="topic-card">
            <div class="topic-name">{name}</div>
            <div class="topic-summary">{summary}</div>
            <div class="topic-summary" style="margin-top:4px">Signal: {direction} &middot; Confidence: {confidence}</div>
        </div>
        """

    exec_summary = _html.escape(narratives.get("executive_summary", ""))
    notable = _html.escape(narratives.get("notable_source", ""))

    markets_to_watch = narratives.get("markets_to_watch", [])
    markets_html = ""
    for m in markets_to_watch[:5]:
        markets_html += f'<div style="margin:6px 0;font-size:10pt">&bull; {_html.escape(m)}</div>'

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><style>{REPORT_CSS}</style></head>
<body>
    <div class="header">
        <div class="title">NARVE.AI INTELLIGENCE REPORT</div>
        <div class="subtitle">
            Week of {ws.strftime('%B %d')} &ndash; {we.strftime('%B %d, %Y')}
            &nbsp;&middot;&nbsp; Prepared for: {_html.escape(data.get('display_name', 'Subscriber'))}
        </div>
    </div>

    <h2>Executive Summary</h2>
    <div class="narrative">{exec_summary}</div>

    <div class="stat-row">
        <div class="stat"><span class="stat-value">{total}</span><span class="stat-label">Predictions Tracked</span></div>
        <div class="stat"><span class="stat-value">{markets}</span><span class="stat-label">Markets</span></div>
        <div class="stat"><span class="stat-value">{hc_pct}</span><span class="stat-label">High-Cred Accuracy</span></div>
        <div class="stat"><span class="stat-value">{correct_count}/{bet_total}</span><span class="stat-label">Best Bets Correct</span></div>
    </div>

    <hr class="divider">

    <h2>Best Bets &mdash; How They Resolved</h2>
    {bets_html if bets_html else '<div style="color:#888;font-size:10pt">No predictions resolved this week.</div>'}

    <h2>Source Performance</h2>
    {sources_html if sources_html else '<div style="color:#888;font-size:10pt">Not enough data this week.</div>'}
    {f'<div class="narrative">{_html.escape(notable)}</div>' if notable else ""}

    <hr class="divider">

    <h2>Markets to Watch</h2>
    {markets_html if markets_html else '<div style="color:#888;font-size:10pt">No upcoming resolutions identified.</div>'}

    {"<hr class='divider'><h2>Your Signal Search Topics</h2>" + topics_html if topics_html else ""}

    <div class="footer">
        narve.ai &middot; Prediction Market Intelligence<br>
        This report was generated automatically on {_dt.datetime.now(_dt.timezone.utc).strftime('%B %d, %Y at %H:%M UTC')}.<br>
        To unsubscribe from weekly reports, update your notification preferences at narve.ai/settings.
    </div>
</body>
</html>"""


def render_pdf(html_content: str) -> bytes:
    """Convert HTML to PDF bytes using WeasyPrint.

    Falls back to returning the raw HTML as bytes if WeasyPrint isn't
    installed, so the feature degrades gracefully.
    """
    try:
        from weasyprint import HTML as WeasyprintHTML
        return WeasyprintHTML(string=html_content).write_pdf()
    except ImportError:
        log.warning("weasyprint not installed — returning HTML as fallback (not a real PDF)")
        return html_content.encode("utf-8")
    except Exception as exc:
        log.exception("PDF render failed: %s", exc)
        return html_content.encode("utf-8")


def save_pdf(user_id: int, week_start: int, pdf_bytes: bytes) -> str:
    """Write PDF to disk and return the relative path."""
    ws = _dt.datetime.fromtimestamp(week_start, tz=_dt.timezone.utc)
    user_dir = REPORTS_DIR / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    filename = f"narve_report_{ws.strftime('%Y-%m-%d')}.pdf"
    path = user_dir / filename
    path.write_bytes(pdf_bytes)
    return str(path.relative_to(REPORTS_DIR.parent) if path.is_relative_to(REPORTS_DIR.parent) else path)


# ── Full pipeline ────────────────────────────────────────────────────────────

async def generate_and_deliver(user_id: int, week_start: int, week_end: int) -> dict:
    """Full pipeline: collect → Claude → PDF → store → email → record.

    Returns a status dict for the job audit log.
    """
    log.info("weekly report: starting for user_id=%d week_start=%d", user_id, week_start)

    # 1. Collect data
    data = collect_report_data(user_id, week_start, week_end)
    if data["total_predictions"] == 0:
        log.info("weekly report: skipping user %d — no data this week", user_id)
        return {"status": "skipped", "reason": "no_data"}

    # 2. Generate narratives via Claude
    narratives = await generate_narratives(data)

    # 3. Render PDF
    html_content = render_report_html(data, narratives)
    pdf_bytes = render_pdf(html_content)

    # 4. Save to disk
    pdf_path = save_pdf(user_id, week_start, pdf_bytes)

    # 5. Record in DB
    resolved = data.get("resolved_predictions", [])
    correct = sum(1 for p in resolved[:10] if p.get("resolved_correct"))
    bet_total = min(len(resolved), 10)
    roi = ((correct / bet_total * 100) - 50) * 0.4 if bet_total > 0 else 0.0

    top_source = data["top_sources"][0]["source_handle"] if data["top_sources"] else None

    report_id = db.upsert_weekly_report(
        user_id=user_id,
        week_start=week_start,
        week_end=week_end,
        pdf_path=pdf_path,
        best_bets_correct=correct,
        best_bets_total=bet_total,
        simulated_roi_pct=round(roi, 2),
        top_source_handle=top_source,
        total_predictions=data["total_predictions"],
        total_markets=data["total_markets"],
        high_cred_accuracy=(
            data["high_cred_correct"] / data["high_cred_total"]
            if data["high_cred_total"] > 0 else None
        ),
    )

    # 6. Email with PDF attached (via job queue)
    user = db.get_user_by_id(user_id)
    if user and user["email"]:
        try:
            from jobs.registry import enqueue_job
            await enqueue_job(
                "send_weekly_report_email",
                user_id=user_id,
                report_id=report_id,
                email=user["email"],
                display_name=data["display_name"],
                week_start=week_start,
                week_end=week_end,
                pdf_path=pdf_path,
            )
            db.mark_report_delivered(report_id)
        except Exception as exc:
            log.error("weekly report: email enqueue failed for user %d: %s", user_id, exc)

    log.info(
        "weekly report: completed for user_id=%d report_id=%d predictions=%d bets=%d/%d",
        user_id, report_id, data["total_predictions"], correct, bet_total,
    )
    return {
        "status": "generated",
        "report_id": report_id,
        "pdf_path": pdf_path,
        "predictions": data["total_predictions"],
        "bets_correct": correct,
        "bets_total": bet_total,
    }

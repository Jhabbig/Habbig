#!/usr/bin/env python3
"""
Weekly email digest — engagement loop for users who haven't installed push.

The digest summarises the last 7 days:
  - Portfolio value + WoW change
  - Execution log (how many DCAs ran, how many filled)
  - Cycle indicators across the 5 tracked assets
  - Tax-loss harvest opportunities (count + total potential savings)
  - Top + bottom performer of the week from the user's holdings
  - Active strategies + their last action

Cadence: cron tick runs hourly, but each user only gets one digest per
week (day-of-week configurable in `crypto_user_preferences`; default
Monday morning UTC).

Send transport reuses `email_alerts.send_email()` which already handles
SMTP_HOST / SMTP_USER / SMTP_PASS env. If SMTP is unconfigured, the
digest no-ops (logs only).
"""

from __future__ import annotations

import html as html_mod
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import database as db
import long_term as lt
import indicators as ind
import tax as tax_mod

try:
    from email_alerts import send_email  # type: ignore
except Exception:
    def send_email(*args, **kwargs):  # type: ignore
        log.warning("email_alerts.send_email unavailable")
        return False

log = logging.getLogger("crypto.digest")


# ─── Content building ───────────────────────────────────────────────────────

def _portfolio_summary(user_id: str) -> dict:
    """Roll-up by ticker with current value + 7d performance."""
    rollup = db.get_holdings_rollup(user_id)
    total_now = 0.0
    total_7d_ago = 0.0
    per_asset = []
    for r in rollup:
        ticker = r["ticker"]
        _, closes = lt.get_daily_closes(ticker, days=10)
        if len(closes) < 8:
            continue
        price_now = float(closes[-1])
        price_7d = float(closes[-8])
        value_now = r["qty"] * price_now
        value_7d = r["qty"] * price_7d
        total_now += value_now
        total_7d_ago += value_7d
        per_asset.append({
            "ticker": ticker, "qty": r["qty"], "price_now": price_now,
            "value_now": value_now, "value_7d_ago": value_7d,
            "change_7d_pct": (price_now / price_7d - 1.0) if price_7d > 0 else 0.0,
            "change_7d_usd": value_now - value_7d,
        })
    per_asset.sort(key=lambda x: x["change_7d_pct"], reverse=True)
    return {
        "total_now": total_now, "total_7d_ago": total_7d_ago,
        "change_7d_usd": total_now - total_7d_ago,
        "change_7d_pct": (total_now / total_7d_ago - 1.0) if total_7d_ago > 0 else 0.0,
        "per_asset": per_asset,
    }


def _execution_summary(user_id: str) -> dict:
    """Last 7 days of execution log activity."""
    rows = db.get_executions_since(user_id, hours=24 * 7)
    placed = sum(1 for r in rows if r["action"] == "placed")
    dry_run = sum(1 for r in rows if r["action"] == "dry_run")
    filled = sum(1 for r in rows if r["status"] == "filled")
    blocked = sum(1 for r in rows if r["action"] == "blocked")
    usd_spent = sum(float(r["usd_amount"] or 0) for r in rows
                    if r["action"] == "placed" and r["side"] == "buy")
    return {
        "placed": placed, "dry_run": dry_run, "filled": filled,
        "blocked": blocked, "usd_spent": usd_spent, "count": len(rows),
    }


def _indicators_summary() -> list[dict]:
    """Composite indicator score for each tracked asset."""
    out = []
    for ticker in lt.TICKER_MAP.keys():
        try:
            comp = ind.composite_score(ticker)
            out.append({"ticker": ticker, **comp})
        except Exception:
            continue
    return out


def _harvest_summary(user_id: str) -> dict:
    """Current tax-loss-harvest opportunities."""
    opps = tax_mod.find_harvest_opportunities(user_id)
    return {
        "count": len(opps),
        "total_loss_usd": sum(o.unrealized_loss_usd for o in opps),
        "total_tax_save_usd": sum(o.estimated_tax_save_usd for o in opps),
    }


def _subscriptions_summary(user_id: str) -> list[dict]:
    rows = db.get_strategy_subscriptions(user_id)
    return [{
        "strategy_name": r["strategy_name"], "base_ticker": r["base_ticker"],
        "last_action": r["last_action"] or "—",
        "last_run_at": r["last_run_at"] or "never",
        "paused": bool(r["paused"]),
    } for r in rows]


def build_digest(user_id: str) -> dict:
    """Compile everything the digest needs into a single dict."""
    return {
        "user_id": user_id,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "portfolio": _portfolio_summary(user_id),
        "execution": _execution_summary(user_id),
        "indicators": _indicators_summary(),
        "harvest": _harvest_summary(user_id),
        "subscriptions": _subscriptions_summary(user_id),
    }


# ─── HTML rendering ─────────────────────────────────────────────────────────

def _fmt_usd(v: float) -> str:
    return f"${v:,.2f}"


def _fmt_pct(v: float) -> str:
    return f"{v*100:+.2f}%"


def _esc(s) -> str:
    return html_mod.escape(str(s))


def render_html(digest: dict) -> str:
    """Inline-styled HTML — most clients still strip <style>. Dark theme
    matched to the dashboard so it doesn't feel jarring."""
    p = digest["portfolio"]
    e = digest["execution"]
    h = digest["harvest"]
    portfolio_color = "#22c55e" if p["change_7d_usd"] >= 0 else "#ef4444"

    per_asset_rows = ""
    for a in p["per_asset"]:
        c = "#22c55e" if a["change_7d_pct"] >= 0 else "#ef4444"
        per_asset_rows += (
            f'<tr><td style="padding:6px 10px;border-bottom:1px solid #222a36">'
            f'<b>{_esc(a["ticker"])}</b></td>'
            f'<td style="padding:6px 10px;text-align:right;border-bottom:1px solid #222a36;color:#e6edf5">{a["qty"]:.6f}</td>'
            f'<td style="padding:6px 10px;text-align:right;border-bottom:1px solid #222a36;color:#e6edf5">{_fmt_usd(a["value_now"])}</td>'
            f'<td style="padding:6px 10px;text-align:right;border-bottom:1px solid #222a36;color:{c}">{_fmt_pct(a["change_7d_pct"])}</td>'
            f'<td style="padding:6px 10px;text-align:right;border-bottom:1px solid #222a36;color:{c}">{_fmt_usd(a["change_7d_usd"])}</td></tr>'
        )

    ind_rows = ""
    for i in digest["indicators"]:
        if i.get("score") is None:
            continue
        label = i.get("label", "—")
        col = {"accumulate": "#22c55e", "lean-bullish": "#22c55e",
               "lean-bearish": "#eab308", "defensive": "#ef4444"}.get(label, "#7d8a99")
        ind_rows += (
            f'<tr><td style="padding:6px 10px;border-bottom:1px solid #222a36"><b>{_esc(i["ticker"])}</b></td>'
            f'<td style="padding:6px 10px;text-align:right;color:{col};border-bottom:1px solid #222a36">{_esc(label.upper())}</td>'
            f'<td style="padding:6px 10px;text-align:right;color:#7d8a99;border-bottom:1px solid #222a36">{i["score"]:.2f}</td></tr>'
        )

    sub_rows = ""
    for s in digest["subscriptions"]:
        status = "Paused" if s["paused"] else "Active"
        sub_rows += (
            f'<tr><td style="padding:6px 10px;border-bottom:1px solid #222a36">{_esc(s["strategy_name"])}</td>'
            f'<td style="padding:6px 10px;text-align:right;border-bottom:1px solid #222a36">{_esc(s["base_ticker"])}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #222a36;font-size:12px;color:#7d8a99">{_esc(s["last_action"])}</td>'
            f'<td style="padding:6px 10px;text-align:right;border-bottom:1px solid #222a36;font-size:12px">{_esc(status)}</td></tr>'
        )

    return f"""<!doctype html>
<html><body style="margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0d12;color:#e6edf5">
<div style="max-width:600px;margin:0 auto;padding:24px">
  <h1 style="margin:0 0 4px;font-size:20px">CryptoEdge — weekly digest</h1>
  <div style="color:#7d8a99;font-size:13px;margin-bottom:18px">{_esc(digest["as_of"][:10])}</div>

  <h2 style="font-size:13px;text-transform:uppercase;letter-spacing:.5px;color:#7d8a99;margin:18px 0 6px">Portfolio</h2>
  <div style="background:#131820;border:1px solid #222a36;border-radius:8px;padding:14px;margin-bottom:14px">
    <div style="display:flex;justify-content:space-between"><span style="color:#7d8a99">Value</span><span style="font-weight:600">{_fmt_usd(p["total_now"])}</span></div>
    <div style="display:flex;justify-content:space-between;margin-top:4px"><span style="color:#7d8a99">7-day change</span><span style="font-weight:600;color:{portfolio_color}">{_fmt_usd(p["change_7d_usd"])} ({_fmt_pct(p["change_7d_pct"])})</span></div>
  </div>
  {f'<table style="width:100%;border-collapse:collapse;background:#131820;border:1px solid #222a36;border-radius:8px;overflow:hidden;font-size:13px"><thead><tr style="background:#1a2029"><th style="padding:8px 10px;text-align:left;color:#7d8a99">Asset</th><th style="padding:8px 10px;text-align:right;color:#7d8a99">Qty</th><th style="padding:8px 10px;text-align:right;color:#7d8a99">Value</th><th style="padding:8px 10px;text-align:right;color:#7d8a99">7d Δ%</th><th style="padding:8px 10px;text-align:right;color:#7d8a99">7d Δ$</th></tr></thead><tbody>{per_asset_rows}</tbody></table>' if per_asset_rows else '<div style="color:#7d8a99;font-size:13px">No holdings yet — add lots in the Portfolio tab.</div>'}

  <h2 style="font-size:13px;text-transform:uppercase;letter-spacing:.5px;color:#7d8a99;margin:24px 0 6px">Execution (last 7 days)</h2>
  <div style="background:#131820;border:1px solid #222a36;border-radius:8px;padding:14px">
    <div style="display:flex;justify-content:space-between"><span style="color:#7d8a99">Orders placed</span><span>{e["placed"]}</span></div>
    <div style="display:flex;justify-content:space-between;margin-top:3px"><span style="color:#7d8a99">Filled</span><span>{e["filled"]}</span></div>
    <div style="display:flex;justify-content:space-between;margin-top:3px"><span style="color:#7d8a99">Dry-run (simulated)</span><span>{e["dry_run"]}</span></div>
    <div style="display:flex;justify-content:space-between;margin-top:3px"><span style="color:#7d8a99">Blocked by safety</span><span>{e["blocked"]}</span></div>
    <div style="display:flex;justify-content:space-between;margin-top:3px"><span style="color:#7d8a99">USD spent</span><span>{_fmt_usd(e["usd_spent"])}</span></div>
  </div>

  <h2 style="font-size:13px;text-transform:uppercase;letter-spacing:.5px;color:#7d8a99;margin:24px 0 6px">Cycle indicators</h2>
  <table style="width:100%;border-collapse:collapse;background:#131820;border:1px solid #222a36;border-radius:8px;overflow:hidden;font-size:13px"><thead><tr style="background:#1a2029"><th style="padding:8px 10px;text-align:left;color:#7d8a99">Asset</th><th style="padding:8px 10px;text-align:right;color:#7d8a99">Signal</th><th style="padding:8px 10px;text-align:right;color:#7d8a99">Score</th></tr></thead><tbody>{ind_rows or '<tr><td colspan="3" style="padding:10px;color:#7d8a99">No data yet — first refresh runs on server startup.</td></tr>'}</tbody></table>

  {f'<h2 style="font-size:13px;text-transform:uppercase;letter-spacing:.5px;color:#7d8a99;margin:24px 0 6px">Tax-loss harvest</h2><div style="background:#131820;border:1px solid #222a36;border-radius:8px;padding:14px"><div style="display:flex;justify-content:space-between"><span style="color:#7d8a99">{h["count"]} opportunity(ies)</span><span style="color:#ef4444;font-weight:600">{_fmt_usd(h["total_loss_usd"])}</span></div><div style="display:flex;justify-content:space-between;margin-top:3px"><span style="color:#7d8a99">Estimated tax savings</span><span style="color:#22c55e;font-weight:600">{_fmt_usd(h["total_tax_save_usd"])}</span></div></div>' if h["count"] else ""}

  {f'<h2 style="font-size:13px;text-transform:uppercase;letter-spacing:.5px;color:#7d8a99;margin:24px 0 6px">Live strategies</h2><table style="width:100%;border-collapse:collapse;background:#131820;border:1px solid #222a36;border-radius:8px;overflow:hidden;font-size:13px"><thead><tr style="background:#1a2029"><th style="padding:8px 10px;text-align:left;color:#7d8a99">Strategy</th><th style="padding:8px 10px;text-align:right;color:#7d8a99">Asset</th><th style="padding:8px 10px;text-align:left;color:#7d8a99">Last action</th><th style="padding:8px 10px;text-align:right;color:#7d8a99">Status</th></tr></thead><tbody>{sub_rows}</tbody></table>' if sub_rows else ""}

  <div style="margin-top:24px;text-align:center"><a href="https://crypto.narve.ai/long-term" style="color:#3b82f6;text-decoration:none">Open dashboard →</a></div>
  <div style="margin-top:18px;color:#7d8a99;font-size:11px;text-align:center">You can disable this digest in <a href="https://crypto.narve.ai/long-term#settings" style="color:#7d8a99">notification settings</a>.</div>
</div></body></html>"""


# ─── Send + cron ────────────────────────────────────────────────────────────

def send_digest_for_user(user_id: str, email: str) -> bool:
    """Build + send. Returns True if SMTP accepted it."""
    digest = build_digest(user_id)
    html = render_html(digest)
    subject = f"CryptoEdge — your weekly summary"
    try:
        ok = send_email(to=email, subject=subject, html=html)
        if ok:
            db.update_user_preference_digest_sent(user_id)
        return bool(ok)
    except Exception as e:
        log.warning("send_digest_for_user failed for %s: %s", user_id, e)
        return False


def find_users_due_for_digest() -> list[dict]:
    """Return rows where digest_enabled=1 AND it's their preferred day-of-week
    AND we haven't sent in the last 6 days (debounce against double-fires)."""
    today_dow = datetime.now(timezone.utc).weekday()  # 0=Mon
    rows = db.get_users_due_for_digest(today_dow, debounce_days=6)
    return rows


def run_digest_tick() -> dict:
    """Cron entry. Send a digest to every user whose preferred day is today
    and who hasn't received one in the last 6 days."""
    due = find_users_due_for_digest()
    sent = 0
    failed = 0
    for row in due:
        if not row.get("email"):
            continue
        if send_digest_for_user(row["user_id"], row["email"]):
            sent += 1
        else:
            failed += 1
    return {"considered": len(due), "sent": sent, "failed": failed}

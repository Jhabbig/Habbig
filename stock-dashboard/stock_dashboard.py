#!/usr/bin/env python3
"""
StockSignal — Consumer-Grade Polymarket Prediction Dashboard

A clean, modern dashboard inspired by Notion/Wispr design language.
Minimal, spacious, soft rounded cards, Inter font, subtle animations.

Run: python3 stock_dashboard.py [--port 8050]
"""

import html
import json
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from http.server import HTTPServer, SimpleHTTPRequestHandler

TRADE_LOG = Path(__file__).parent / "stock_trades.json"
BOT_LOG = Path(__file__).parent / "stock_bot_activity.log"
DASHBOARD_PORT = 8050


def load_state():
    """Load bot state from JSON, retrying once on decode error (race condition)."""
    for attempt in range(2):
        if not TRADE_LOG.exists():
            return None
        try:
            return json.loads(TRADE_LOG.read_text())
        except json.JSONDecodeError:
            if attempt == 0:
                time.sleep(0.1)  # brief wait for atomic rename to complete
                continue
            return None
        except Exception:
            return None


def get_recent_logs(n=50):
    if not BOT_LOG.exists():
        return []
    try:
        lines = BOT_LOG.read_text().strip().split("\n")
        return lines[-n:]
    except Exception:
        return []


def build_html():
    state = load_state()
    logs = get_recent_logs(80)

    if state is None:
        state = {
            "balance": 10000, "total_trades": 0, "wins": 0, "losses": 0,
            "total_pnl": 0, "peak_balance": 10000, "pending": {},
            "trades": [], "daily_bets": 0,
        }

    balance = state.get("balance", 10000)
    total_trades = state.get("total_trades", 0)
    wins = state.get("wins", 0)
    losses = state.get("losses", 0)
    pnl = state.get("total_pnl", 0)
    peak = state.get("peak_balance", 10000)
    pending = state.get("pending", {})
    trades = state.get("trades", [])
    daily_bets = state.get("daily_bets", 0)

    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    drawdown = ((peak - balance) / peak * 100) if peak > 0 else 0
    pnl_pct = (pnl / 10000) * 100

    # Determine bot status from logs
    bot_status = "Offline"
    bot_status_color = "#94a3b8"
    if logs:
        last_log = logs[-1].lower()
        if "sleeping" in last_log or "market closed" in last_log:
            bot_status = "Sleeping"
            bot_status_color = "#f59e0b"
        elif "cycle start" in last_log or "discovering" in last_log:
            bot_status = "Active"
            bot_status_color = "#22c55e"
        else:
            bot_status = "Running"
            bot_status_color = "#22c55e"

    # Ticker performance summary from trades
    ticker_stats = {}
    for t in trades:
        tk = t.get("ticker", "?").upper()
        if tk not in ticker_stats:
            ticker_stats[tk] = {"wins": 0, "losses": 0, "pnl": 0}
        if t.get("won"):
            ticker_stats[tk]["wins"] += 1
        else:
            ticker_stats[tk]["losses"] += 1
        ticker_stats[tk]["pnl"] += t.get("pnl", 0)

    # Build prediction cards for pending bets
    prediction_cards = ""
    for ticker, bet in sorted(pending.items()):
        signals = bet.get("signals", {})
        conf = bet.get("confidence", 0.5)
        direction = bet.get("direction", "up")
        is_up = direction == "up"
        dir_color = "#22c55e" if is_up else "#ef4444"
        dir_icon = "&#8593;" if is_up else "&#8595;"
        dir_label = "Bullish" if is_up else "Bearish"
        ticker_safe = html.escape(ticker.upper())
        conf_pct = conf * 100

        # Confidence ring: SVG donut
        ring_radius = 36
        ring_circumference = 2 * 3.14159 * ring_radius
        ring_offset = ring_circumference * (1 - conf)

        signal_pills = ""
        if "rsi" in signals:
            rsi = signals["rsi"]
            rsi_color = "#22c55e" if rsi < 30 else "#ef4444" if rsi > 70 else "#94a3b8"
            signal_pills += f'<span class="pill" style="color:{rsi_color}">RSI {rsi:.0f}</span>'
        if "macd_hist" in signals:
            m = signals["macd_hist"]
            signal_pills += f'<span class="pill">MACD {"+" if m > 0 else "-"}</span>'
        if "premarket_pct" in signals:
            pm = signals["premarket_pct"]
            pm_color = "#22c55e" if pm > 0 else "#ef4444"
            signal_pills += f'<span class="pill" style="color:{pm_color}">Pre {pm:+.1f}%</span>'
        if "up_probability" in signals:
            signal_pills += f'<span class="pill">ML {signals["up_probability"]:.0%}</span>'

        prediction_cards += f"""
        <div class="pred-card">
            <div class="pred-header">
                <div class="pred-ticker">{ticker_safe}</div>
                <div class="pred-direction" style="color:{dir_color}">{dir_icon} {dir_label}</div>
            </div>
            <div class="pred-body">
                <div class="confidence-ring">
                    <svg viewBox="0 0 80 80">
                        <circle cx="40" cy="40" r="{ring_radius}" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="6"/>
                        <circle cx="40" cy="40" r="{ring_radius}" fill="none" stroke="{dir_color}" stroke-width="6"
                            stroke-dasharray="{ring_circumference:.1f}" stroke-dashoffset="{ring_offset:.1f}"
                            stroke-linecap="round" transform="rotate(-90 40 40)"
                            style="transition: stroke-dashoffset 1s ease;"/>
                        <text x="40" y="38" text-anchor="middle" fill="white" font-size="14" font-weight="600">{conf_pct:.0f}%</text>
                        <text x="40" y="50" text-anchor="middle" fill="#94a3b8" font-size="8">conf</text>
                    </svg>
                </div>
                <div class="pred-details">
                    <div class="pred-meta">
                        <span class="meta-label">Edge</span>
                        <span class="meta-value" style="color:#a78bfa">+{bet.get('edge', 0):.1%}</span>
                    </div>
                    <div class="pred-meta">
                        <span class="meta-label">Bet</span>
                        <span class="meta-value">${bet.get('bet_size', 0):.0f}</span>
                    </div>
                    <div class="pred-meta">
                        <span class="meta-label">Market</span>
                        <span class="meta-value">{bet.get('market_prob', 0.5):.0%}</span>
                    </div>
                </div>
            </div>
            <div class="pred-signals">{signal_pills}</div>
        </div>"""

    if not prediction_cards:
        prediction_cards = """
        <div class="empty-state">
            <div class="empty-icon">&#9684;</div>
            <div class="empty-title">No Active Predictions</div>
            <div class="empty-sub">The bot will place new predictions at the next market cycle</div>
        </div>"""

    # Build trade history rows
    trade_rows = ""
    for t in reversed(trades[-50:]):
        won = t.get("won", False)
        result_class = "won" if won else "lost"
        result_icon = "&#10003;" if won else "&#10007;"
        pnl_val = t.get("pnl", 0)
        pnl_sign = "+" if pnl_val >= 0 else ""
        is_up = t.get("direction") == "up"
        actual_up = t.get("actual") == "up"
        t_ticker = html.escape(t.get('ticker', '?').upper())
        t_direction = html.escape(t.get('direction', '?').upper())
        t_actual = html.escape(t.get('actual', '?').upper())
        t_date = html.escape(str(t.get('date', 'N/A')))

        trade_rows += f"""
        <tr class="trade-row {result_class}">
            <td><span class="trade-ticker">{t_ticker}</span></td>
            <td><span class="dir-badge {'up' if is_up else 'down'}">{t_direction}</span></td>
            <td><span class="dir-badge {'up' if actual_up else 'down'}">{t_actual}</span></td>
            <td><span class="result-badge {result_class}">{result_icon}</span></td>
            <td>{t.get('confidence', 0):.0%}</td>
            <td class="{'profit-text' if pnl_val >= 0 else 'loss-text'}">{pnl_sign}${abs(pnl_val):.2f}</td>
            <td>${t.get('balance_after', 0):,.2f}</td>
            <td class="date-cell">{t_date}</td>
        </tr>"""

    if not trade_rows:
        trade_rows = '<tr><td colspan="8" class="empty-table">No completed trades yet</td></tr>'

    # Build per-ticker performance cards
    ticker_perf_cards = ""
    for tk, st in sorted(ticker_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
        total = st["wins"] + st["losses"]
        wr = (st["wins"] / total * 100) if total > 0 else 0
        pnl_c = "profit-text" if st["pnl"] >= 0 else "loss-text"
        bar_width = min(wr, 100)
        tk_safe = html.escape(tk)
        ticker_perf_cards += f"""
        <div class="ticker-perf">
            <div class="tp-header">
                <span class="tp-name">{tk_safe}</span>
                <span class="tp-pnl {pnl_c}">{"+" if st["pnl"] >= 0 else ""}${st["pnl"]:.2f}</span>
            </div>
            <div class="tp-bar-bg"><div class="tp-bar" style="width:{bar_width}%"></div></div>
            <div class="tp-stats">{st["wins"]}W {st["losses"]}L &middot; {wr:.0f}% win rate</div>
        </div>"""

    # Balance chart data
    chart_balances = [10000]
    for t in trades:
        chart_balances.append(t.get("balance_after", chart_balances[-1]))

    chart_svg = build_svg_chart(chart_balances)

    # Log entries (last 30)
    log_entries = ""
    for line in logs[-30:]:
        # Color code log lines
        css = "log-normal"
        if "predict" in line.lower():
            css = "log-predict"
        elif "win" in line.lower() or "correct" in line.lower():
            css = "log-win"
        elif "loss" in line.lower() or "wrong" in line.lower():
            css = "log-loss"
        elif "sleeping" in line.lower() or "market closed" in line.lower():
            css = "log-sleep"
        log_entries += f'<div class="log-entry {css}">{html.escape(line)}</div>'

    # Current time
    now_utc = datetime.now(timezone.utc)
    now_et = datetime.now(ZoneInfo("America/New_York"))

    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="60">
<title>StockSignal — AI Prediction Dashboard</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

:root {{
    --bg: #fafbfc;
    --surface: #ffffff;
    --surface-hover: #f8f9fb;
    --border: #e8ecf1;
    --border-light: #f0f2f5;
    --text-primary: #1a1d23;
    --text-secondary: #6b7280;
    --text-muted: #9ca3af;
    --accent: #6366f1;
    --accent-light: #eef2ff;
    --green: #10b981;
    --green-light: #ecfdf5;
    --green-bg: rgba(16, 185, 129, 0.08);
    --red: #ef4444;
    --red-light: #fef2f2;
    --red-bg: rgba(239, 68, 68, 0.08);
    --purple: #8b5cf6;
    --purple-light: #f5f3ff;
    --amber: #f59e0b;
    --amber-light: #fffbeb;
    --shadow-sm: 0 1px 2px rgba(0,0,0,0.04);
    --shadow-md: 0 4px 12px rgba(0,0,0,0.06);
    --shadow-lg: 0 8px 24px rgba(0,0,0,0.08);
    --radius: 16px;
    --radius-sm: 10px;
    --radius-xs: 6px;
}}

* {{ margin: 0; padding: 0; box-sizing: border-box; }}

body {{
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--bg);
    color: var(--text-primary);
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
}}

/* ─── Navbar ──────────────────────────────────────── */
.navbar {{
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 0 32px;
    height: 64px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 100;
    backdrop-filter: blur(12px);
    background: rgba(255,255,255,0.88);
}}
.nav-brand {{
    display: flex;
    align-items: center;
    gap: 10px;
}}
.nav-logo {{
    width: 32px;
    height: 32px;
    background: linear-gradient(135deg, #6366f1, #8b5cf6);
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: white;
    font-weight: 700;
    font-size: 14px;
}}
.nav-title {{
    font-size: 16px;
    font-weight: 600;
    color: var(--text-primary);
}}
.nav-subtitle {{
    font-size: 11px;
    color: var(--text-muted);
    font-weight: 400;
}}
.nav-right {{
    display: flex;
    align-items: center;
    gap: 16px;
}}
.status-pill {{
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 14px;
    border-radius: 20px;
    font-size: 12px;
    font-weight: 500;
    background: var(--surface-hover);
    border: 1px solid var(--border);
}}
.status-dot {{
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: {bot_status_color};
    animation: pulse 2.5s ease-in-out infinite;
}}
@keyframes pulse {{
    0%, 100% {{ opacity: 1; transform: scale(1); }}
    50% {{ opacity: 0.5; transform: scale(0.85); }}
}}
.nav-time {{
    font-size: 12px;
    color: var(--text-muted);
    font-weight: 400;
}}

/* ─── Layout ──────────────────────────────────────── */
.container {{
    max-width: 1280px;
    margin: 0 auto;
    padding: 28px 32px 60px;
}}

/* ─── Hero Stats ──────────────────────────────────── */
.hero {{
    display: grid;
    grid-template-columns: 2fr 1fr 1fr 1fr;
    gap: 16px;
    margin-bottom: 28px;
}}
.hero-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 24px;
    box-shadow: var(--shadow-sm);
    transition: box-shadow 0.2s, transform 0.2s;
}}
.hero-card:hover {{
    box-shadow: var(--shadow-md);
    transform: translateY(-1px);
}}
.hero-card.featured {{
    background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%);
    color: white;
    border: none;
}}
.hero-card.featured .hero-label {{ color: rgba(255,255,255,0.7); }}
.hero-card.featured .hero-sub {{ color: rgba(255,255,255,0.6); }}
.hero-label {{
    font-size: 12px;
    font-weight: 500;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 8px;
}}
.hero-value {{
    font-size: 28px;
    font-weight: 700;
    letter-spacing: -0.5px;
    line-height: 1.1;
}}
.hero-sub {{
    font-size: 12px;
    color: var(--text-muted);
    margin-top: 6px;
    font-weight: 400;
}}
.profit-text {{ color: var(--green); }}
.loss-text {{ color: var(--red); }}

/* ─── Section ─────────────────────────────────────── */
.section {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow-sm);
    margin-bottom: 20px;
    overflow: hidden;
    transition: box-shadow 0.2s;
}}
.section:hover {{ box-shadow: var(--shadow-md); }}
.section-head {{
    padding: 18px 24px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid var(--border-light);
}}
.section-title {{
    font-size: 15px;
    font-weight: 600;
    color: var(--text-primary);
}}
.section-badge {{
    font-size: 11px;
    font-weight: 500;
    color: var(--text-muted);
    background: var(--surface-hover);
    border: 1px solid var(--border);
    padding: 4px 12px;
    border-radius: 20px;
}}

/* ─── Prediction Cards ────────────────────────────── */
.pred-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 14px;
    padding: 20px 24px;
}}
.pred-card {{
    background: var(--surface-hover);
    border: 1px solid var(--border-light);
    border-radius: var(--radius-sm);
    padding: 18px;
    transition: all 0.2s;
}}
.pred-card:hover {{
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-light);
}}
.pred-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 14px;
}}
.pred-ticker {{
    font-size: 16px;
    font-weight: 700;
    color: var(--text-primary);
}}
.pred-direction {{
    font-size: 13px;
    font-weight: 600;
}}
.pred-body {{
    display: flex;
    align-items: center;
    gap: 16px;
    margin-bottom: 12px;
}}
.confidence-ring {{
    flex-shrink: 0;
    width: 72px;
    height: 72px;
}}
.confidence-ring svg {{
    width: 100%;
    height: 100%;
}}
.confidence-ring text {{
    font-family: 'Inter', sans-serif;
}}
.pred-details {{
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 6px;
}}
.pred-meta {{
    display: flex;
    justify-content: space-between;
    align-items: center;
}}
.meta-label {{
    font-size: 11px;
    color: var(--text-muted);
    font-weight: 500;
}}
.meta-value {{
    font-size: 13px;
    font-weight: 600;
    color: var(--text-primary);
}}
.pred-signals {{
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
}}
.pill {{
    font-size: 10px;
    font-weight: 500;
    padding: 3px 8px;
    border-radius: 4px;
    background: rgba(0,0,0,0.04);
    color: var(--text-secondary);
    white-space: nowrap;
}}

.empty-state {{
    text-align: center;
    padding: 48px 24px;
}}
.empty-icon {{
    font-size: 40px;
    color: var(--text-muted);
    margin-bottom: 12px;
    opacity: 0.4;
}}
.empty-title {{
    font-size: 15px;
    font-weight: 600;
    color: var(--text-secondary);
    margin-bottom: 6px;
}}
.empty-sub {{
    font-size: 13px;
    color: var(--text-muted);
}}

/* ─── Chart ───────────────────────────────────────── */
.chart-wrap {{
    padding: 20px 24px;
    height: 220px;
}}
.chart-svg {{
    width: 100%;
    height: 100%;
}}

/* ─── Two-col layout ──────────────────────────────── */
.grid-2 {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    margin-bottom: 20px;
}}

/* ─── Table ───────────────────────────────────────── */
.table-wrap {{
    overflow-x: auto;
}}
table {{
    width: 100%;
    border-collapse: collapse;
}}
thead th {{
    text-align: left;
    padding: 10px 20px;
    font-size: 11px;
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    border-bottom: 1px solid var(--border-light);
    background: var(--surface-hover);
}}
tbody td {{
    padding: 12px 20px;
    font-size: 13px;
    color: var(--text-secondary);
    border-bottom: 1px solid var(--border-light);
}}
tbody tr {{ transition: background 0.15s; }}
tbody tr:hover {{ background: var(--surface-hover); }}
.trade-ticker {{
    font-weight: 600;
    color: var(--text-primary);
}}
.dir-badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
}}
.dir-badge.up {{
    color: var(--green);
    background: var(--green-bg);
}}
.dir-badge.down {{
    color: var(--red);
    background: var(--red-bg);
}}
.result-badge {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    border-radius: 50%;
    font-size: 12px;
    font-weight: 700;
}}
.result-badge.won {{
    color: var(--green);
    background: var(--green-bg);
}}
.result-badge.lost {{
    color: var(--red);
    background: var(--red-bg);
}}
.date-cell {{ color: var(--text-muted); font-size: 12px; }}
.empty-table {{
    text-align: center;
    color: var(--text-muted);
    padding: 40px !important;
    font-style: italic;
}}

/* ─── Ticker Performance ──────────────────────────── */
.ticker-grid {{
    padding: 20px 24px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    max-height: 400px;
    overflow-y: auto;
}}
.ticker-perf {{
    padding: 0;
}}
.tp-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 6px;
}}
.tp-name {{
    font-size: 13px;
    font-weight: 600;
    color: var(--text-primary);
}}
.tp-pnl {{
    font-size: 13px;
    font-weight: 600;
}}
.tp-bar-bg {{
    height: 6px;
    background: var(--border-light);
    border-radius: 3px;
    overflow: hidden;
    margin-bottom: 4px;
}}
.tp-bar {{
    height: 100%;
    background: linear-gradient(90deg, var(--accent), var(--purple));
    border-radius: 3px;
    transition: width 0.8s ease;
}}
.tp-stats {{
    font-size: 11px;
    color: var(--text-muted);
}}

/* ─── Activity Log ────────────────────────────────── */
.log-wrap {{
    padding: 16px 24px;
    max-height: 320px;
    overflow-y: auto;
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
}}
.log-entry {{
    font-size: 11.5px;
    line-height: 1.7;
    color: var(--text-muted);
    white-space: pre-wrap;
    word-break: break-all;
}}
.log-predict {{ color: var(--accent); }}
.log-win {{ color: var(--green); }}
.log-loss {{ color: var(--red); }}
.log-sleep {{ color: var(--amber); }}

/* ─── Footer ──────────────────────────────────────── */
.footer {{
    text-align: center;
    padding: 32px;
    font-size: 12px;
    color: var(--text-muted);
}}
.footer a {{
    color: var(--accent);
    text-decoration: none;
}}

/* ─── Responsive ──────────────────────────────────── */
@media (max-width: 1024px) {{
    .hero {{ grid-template-columns: 1fr 1fr; }}
}}
@media (max-width: 768px) {{
    .container {{ padding: 16px; }}
    .navbar {{ padding: 0 16px; }}
    .hero {{ grid-template-columns: 1fr; }}
    .grid-2 {{ grid-template-columns: 1fr; }}
    .pred-grid {{ grid-template-columns: 1fr; }}
    .hero-value {{ font-size: 22px; }}
}}

/* ─── Scrollbar ───────────────────────────────────── */
::-webkit-scrollbar {{ width: 6px; }}
::-webkit-scrollbar-track {{ background: transparent; }}
::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 3px; }}
::-webkit-scrollbar-thumb:hover {{ background: var(--text-muted); }}

/* ─── Animations ──────────────────────────────────── */
@keyframes fadeIn {{
    from {{ opacity: 0; transform: translateY(8px); }}
    to {{ opacity: 1; transform: translateY(0); }}
}}
.hero-card, .section, .pred-card {{
    animation: fadeIn 0.4s ease backwards;
}}
.hero-card:nth-child(1) {{ animation-delay: 0.05s; }}
.hero-card:nth-child(2) {{ animation-delay: 0.1s; }}
.hero-card:nth-child(3) {{ animation-delay: 0.15s; }}
.hero-card:nth-child(4) {{ animation-delay: 0.2s; }}
</style>
</head>
<body>

<!-- Navbar -->
<nav class="navbar">
    <div class="nav-brand">
        <div class="nav-logo">S</div>
        <div>
            <div class="nav-title">StockSignal</div>
            <div class="nav-subtitle">AI-Powered Market Predictions</div>
        </div>
    </div>
    <div class="nav-right">
        <div class="status-pill">
            <div class="status-dot"></div>
            {bot_status}
        </div>
        <div class="nav-time">{now_et.strftime('%b %d, %I:%M %p')} ET</div>
    </div>
</nav>

<div class="container">

<!-- Hero Stats -->
<div class="hero">
    <div class="hero-card featured">
        <div class="hero-label">Portfolio Balance</div>
        <div class="hero-value">${balance:,.2f}</div>
        <div class="hero-sub">{"+" if pnl >= 0 else ""}{pnl_pct:.2f}% all time &middot; Started $10,000</div>
    </div>
    <div class="hero-card">
        <div class="hero-label">Total P&L</div>
        <div class="hero-value {'profit-text' if pnl >= 0 else 'loss-text'}">{"+" if pnl >= 0 else ""}${pnl:,.2f}</div>
        <div class="hero-sub">${(pnl/total_trades if total_trades else 0):,.2f} avg per trade</div>
    </div>
    <div class="hero-card">
        <div class="hero-label">Win Rate</div>
        <div class="hero-value">{win_rate:.1f}%</div>
        <div class="hero-sub">{wins}W &middot; {losses}L &middot; {total_trades} total</div>
    </div>
    <div class="hero-card">
        <div class="hero-label">Active Bets</div>
        <div class="hero-value">{len(pending)}</div>
        <div class="hero-sub">{daily_bets} placed today &middot; Peak ${peak:,.0f}</div>
    </div>
</div>

<!-- Active Predictions -->
<div class="section">
    <div class="section-head">
        <div class="section-title">Active Predictions</div>
        <div class="section-badge">{len(pending)} pending</div>
    </div>
    <div class="pred-grid">
        {prediction_cards}
    </div>
</div>

<!-- Chart + Ticker Performance -->
<div class="grid-2">
    <div class="section">
        <div class="section-head">
            <div class="section-title">Balance History</div>
            <div class="section-badge">{len(chart_balances)} points</div>
        </div>
        <div class="chart-wrap">
            {chart_svg}
        </div>
    </div>
    <div class="section">
        <div class="section-head">
            <div class="section-title">Ticker Performance</div>
            <div class="section-badge">{len(ticker_stats)} stocks</div>
        </div>
        <div class="ticker-grid">
            {ticker_perf_cards if ticker_perf_cards else '<div class="empty-state"><div class="empty-sub">No data yet</div></div>'}
        </div>
    </div>
</div>

<!-- Trade History -->
<div class="section">
    <div class="section-head">
        <div class="section-title">Trade History</div>
        <div class="section-badge">{total_trades} completed</div>
    </div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>Ticker</th>
                    <th>Predicted</th>
                    <th>Actual</th>
                    <th>Result</th>
                    <th>Confidence</th>
                    <th>P/L</th>
                    <th>Balance</th>
                    <th>Date</th>
                </tr>
            </thead>
            <tbody>
                {trade_rows}
            </tbody>
        </table>
    </div>
</div>

<!-- Bot Activity -->
<div class="section">
    <div class="section-head">
        <div class="section-title">Bot Activity</div>
        <div class="section-badge">live log</div>
    </div>
    <div class="log-wrap">
        {log_entries}
    </div>
</div>

</div>

<div class="footer">
    StockSignal &mdash; AI predictions for Polymarket stock markets &middot; Auto-refreshes every 60s
</div>

</body>
</html>"""
    return page_html


def build_svg_chart(balances):
    if len(balances) < 2:
        return '<div style="text-align:center;color:#9ca3af;padding:60px;font-size:14px;">Waiting for first trade...</div>'

    w, h = 800, 180
    pad_x, pad_y = 55, 24
    chart_w = w - pad_x * 2
    chart_h = h - pad_y * 2

    min_val = min(balances) * 0.998
    max_val = max(balances) * 1.002
    val_range = max_val - min_val if max_val != min_val else 1

    points = []
    for i, b in enumerate(balances):
        x = pad_x + (i / (len(balances) - 1)) * chart_w
        y = pad_y + chart_h - ((b - min_val) / val_range) * chart_h
        points.append(f"{x:.1f},{y:.1f}")

    poly_line = " ".join(points)
    fill_points = f"{pad_x},{pad_y + chart_h} " + poly_line + f" {pad_x + chart_w},{pad_y + chart_h}"

    up = balances[-1] >= balances[0]
    color = "#10b981" if up else "#ef4444"
    fill_color = "rgba(16,185,129,0.08)" if up else "rgba(239,68,68,0.08)"

    grid = ""
    for i in range(5):
        gy = pad_y + (i / 4) * chart_h
        val = max_val - (i / 4) * val_range
        grid += f'<line x1="{pad_x}" y1="{gy:.1f}" x2="{w - pad_x}" y2="{gy:.1f}" stroke="#e8ecf1" stroke-width="1"/>'
        grid += f'<text x="{pad_x - 10}" y="{gy + 4:.1f}" text-anchor="end" fill="#9ca3af" font-size="10" font-family="Inter,sans-serif">${val:,.0f}</text>'

    start_y = pad_y + chart_h - ((10000 - min_val) / val_range) * chart_h
    start_line = f'<line x1="{pad_x}" y1="{start_y:.1f}" x2="{w - pad_x}" y2="{start_y:.1f}" stroke="#d1d5db" stroke-width="1" stroke-dasharray="4,4"/>'

    last_x = points[-1].split(",")[0]
    last_y = points[-1].split(",")[1]

    return f"""<svg viewBox="0 0 {w} {h}" class="chart-svg" preserveAspectRatio="none">
        {grid}
        {start_line}
        <polygon points="{fill_points}" fill="{fill_color}"/>
        <polyline points="{poly_line}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
        <circle cx="{last_x}" cy="{last_y}" r="4" fill="{color}" stroke="white" stroke-width="2"/>
    </svg>"""


class DashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            html = build_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(html.encode())
        elif self.path == "/api/state":
            state = load_state() or {}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(state).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description="StockSignal Dashboard")
    parser.add_argument("--port", type=int, default=DASHBOARD_PORT)
    args = parser.parse_args()

    server = HTTPServer(("0.0.0.0", args.port), DashboardHandler)
    print(f"StockSignal dashboard running at http://0.0.0.0:{args.port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped")
        server.server_close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
StockSignal — Consumer-Grade Polymarket Prediction Dashboard

A clean, modern dashboard inspired by Notion/Wispr design language.
Minimal, spacious, soft rounded cards, Inter font, subtle animations.

Run: python3 stock_dashboard.py [--port 8050]
"""

import hmac
import html
import json
import logging
import os
import time
import argparse
import urllib.request
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from http.server import HTTPServer, SimpleHTTPRequestHandler

# ── Layered .env loader ──────────────────────────────────────────────────────
# See sports-dashboard for rationale. ~/.gateway_env → gateway/.env.production →
# dashboard/.env.production → dashboard/.env (first definition wins).
try:
    from dotenv import load_dotenv as _dotenv_load
except ImportError:
    def _dotenv_load(p, override=False):
        for raw in Path(p).read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if not override and k in os.environ:
                continue
            os.environ[k] = v
        return True
_DASHBOARD_DIR = Path(__file__).resolve().parent
_GATEWAY_ENV = None
for _p in [_DASHBOARD_DIR, *_DASHBOARD_DIR.parents][:5]:
    _candidate = _p / "gateway" / ".env.production"
    if _candidate.is_file():
        _GATEWAY_ENV = _candidate
        break
_ENV_SEARCH = [Path.home() / ".gateway_env"]
if _GATEWAY_ENV is not None:
    _ENV_SEARCH.append(_GATEWAY_ENV)
_ENV_SEARCH.extend([_DASHBOARD_DIR / ".env.production", _DASHBOARD_DIR / ".env"])
_loaded_env_files: list[str] = []
for _f in _ENV_SEARCH:
    if _f.is_file():
        _dotenv_load(_f, override=False)
        _loaded_env_files.append(str(_f))
print(f"[stock-dashboard] env files loaded: {len(_loaded_env_files)}", flush=True)
for _f in _loaded_env_files:
    print(f"  ✓ {_f}", flush=True)

_sso_secret = os.environ.get("GATEWAY_SSO_SECRET", "")
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"
if not _sso_secret:
    if _DEV_MODE:
        logging.warning("GATEWAY_SSO_SECRET not set — stock dashboard running in DEV_MODE (no auth)")
    else:
        logging.warning("GATEWAY_SSO_SECRET not set and DEV_MODE not enabled — rejecting all requests")

TRADE_LOG = Path(__file__).parent / "stock_trades.json"
BOT_LOG = Path(__file__).parent / "stock_bot_activity.log"
DASHBOARD_PORT = 8050

# ─── FX rates cache ──────────────────────────────────────────────────
_FX_CACHE: dict = {"data": None, "fetched_at": 0.0}
_FX_TTL = 3600  # 1 hour
_FX_FALLBACK = {
    "base": "USD",
    "date": "fallback",
    "rates": {
        "USD": 1.0, "EUR": 0.92, "GBP": 0.79, "JPY": 150.0, "AUD": 1.52,
        "CAD": 1.36, "CHF": 0.88, "CNY": 7.20, "HKD": 7.83, "NZD": 1.65,
        "SEK": 10.5, "KRW": 1340.0, "SGD": 1.34, "NOK": 10.6, "MXN": 17.0,
        "INR": 83.0, "ZAR": 18.5, "TRY": 32.0, "BRL": 5.0, "DKK": 6.85,
        "PLN": 3.95, "THB": 35.0, "IDR": 15700.0, "HUF": 360.0, "CZK": 23.0,
        "ILS": 3.7, "PHP": 56.0, "MYR": 4.7, "RON": 4.6, "ISK": 137.0,
    },
}


def get_fx_rates() -> dict:
    """Return USD-base FX rates, cached for 1h. Source: frankfurter.dev."""
    now = time.time()
    cached = _FX_CACHE["data"]
    if cached and (now - _FX_CACHE["fetched_at"]) < _FX_TTL:
        return cached
    try:
        req = urllib.request.Request(
            "https://api.frankfurter.dev/v1/latest?base=USD",
            headers={"User-Agent": "narve-stock/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                data.setdefault("rates", {})
                data["rates"]["USD"] = 1.0
                _FX_CACHE["data"] = data
                _FX_CACHE["fetched_at"] = now
                return data
    except Exception as e:
        logging.warning("FX rate fetch failed: %s", e)
    return cached or _FX_FALLBACK


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
    starting_balance = state.get("starting_balance", 10000)
    pnl_pct = (pnl / starting_balance) * 100 if starting_balance else 0

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
    chart_balances = [state.get("starting_balance", 10000)]
    for t in trades:
        chart_balances.append(t.get("balance_after", chart_balances[-1]))

    chart_svg = build_svg_chart(chart_balances, state)

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

/* ─── Trading Panel ──────────────────────────────── */
.connect-btn {{
    display: inline-block;
    margin-top: 16px;
    padding: 10px 24px;
    background: var(--accent);
    color: white;
    border-radius: var(--radius-sm);
    text-decoration: none;
    font-weight: 500;
    font-size: 13px;
    transition: opacity 0.2s;
}}
.connect-btn:hover {{ opacity: 0.85; }}

.broker-account {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 12px;
    margin-bottom: 20px;
    padding: 16px;
    background: var(--surface-hover);
    border-radius: var(--radius-sm);
    border: 1px solid var(--border-light);
}}
.broker-stat-label {{
    font-size: 11px;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    display: block;
}}
.broker-stat-value {{
    font-size: 18px;
    font-weight: 600;
    color: var(--text-primary);
    display: block;
    margin-top: 2px;
}}
.trade-form {{
    padding: 16px;
    background: var(--surface-hover);
    border-radius: var(--radius-sm);
    border: 1px solid var(--border-light);
}}
.trade-form-title {{
    font-size: 13px;
    font-weight: 600;
    color: var(--text-secondary);
    margin-bottom: 12px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}}
.trade-form-row {{
    display: flex;
    gap: 8px;
    margin-bottom: 10px;
    align-items: center;
}}
.trade-form-row:last-child {{ margin-bottom: 0; }}
.trade-input {{
    flex: 1;
    padding: 10px 14px;
    border: 1px solid var(--border);
    border-radius: var(--radius-xs);
    font-size: 14px;
    font-family: inherit;
    background: var(--surface);
    color: var(--text-primary);
    outline: none;
    transition: border-color 0.2s;
}}
.trade-input:focus {{ border-color: var(--accent); }}
.trade-input-sm {{ max-width: 120px; }}
.trade-select {{
    padding: 10px 14px;
    border: 1px solid var(--border);
    border-radius: var(--radius-xs);
    font-size: 14px;
    font-family: inherit;
    background: var(--surface);
    color: var(--text-primary);
    cursor: pointer;
}}
.trade-btn {{
    padding: 10px 20px;
    border: none;
    border-radius: var(--radius-xs);
    font-size: 13px;
    font-weight: 600;
    font-family: inherit;
    cursor: pointer;
    transition: opacity 0.2s;
}}
.trade-btn:hover {{ opacity: 0.85; }}
.trade-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
.trade-btn-buy {{ background: var(--green); color: white; flex: 1; }}
.trade-btn-sell {{ background: var(--red); color: white; flex: 1; }}
.trade-btn-quote {{ background: var(--accent); color: white; white-space: nowrap; }}
.quote-result {{
    padding: 10px 14px;
    margin-bottom: 10px;
    border-radius: var(--radius-xs);
    font-size: 13px;
    background: var(--accent-light);
    border: 1px solid var(--accent);
    color: var(--text-primary);
}}
.trade-result {{
    padding: 10px 14px;
    margin-top: 10px;
    border-radius: var(--radius-xs);
    font-size: 13px;
}}
.trade-result.success {{ background: var(--green-light); border: 1px solid var(--green); }}
.trade-result.error {{ background: var(--red-light); border: 1px solid var(--red); }}
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

<!-- ─── Broker Trading ─────────────────────────────────── -->
<div class="section" id="trading-section">
    <div class="section-head">
        <div class="section-title">Stock Trading</div>
        <div class="section-badge" id="broker-status-badge">checking...</div>
    </div>

    <div id="broker-not-connected" style="display:none;">
        <div class="empty-state">
            <div class="empty-icon">&#9889;</div>
            <div class="empty-title">No Broker Connected</div>
            <div class="empty-sub">Connect your Alpaca account to trade stocks directly from this dashboard.</div>
            <a href="/settings#trading" class="connect-btn">Connect Alpaca</a>
        </div>
    </div>

    <div id="broker-connected" style="display:none;">
        <!-- Account summary -->
        <div class="broker-account" id="broker-account">
            <div class="broker-stat">
                <span class="broker-stat-label">Cash</span>
                <span class="broker-stat-value" id="acct-cash">-</span>
            </div>
            <div class="broker-stat">
                <span class="broker-stat-label">Portfolio</span>
                <span class="broker-stat-value" id="acct-portfolio">-</span>
            </div>
            <div class="broker-stat">
                <span class="broker-stat-label">Buying Power</span>
                <span class="broker-stat-value" id="acct-buying-power">-</span>
            </div>
            <div class="broker-stat">
                <span class="broker-stat-label">Mode</span>
                <span class="broker-stat-value" id="acct-mode">-</span>
            </div>
        </div>

        <!-- Trade form -->
        <div class="trade-form">
            <div class="trade-form-title">Place Order</div>
            <div class="trade-form-row">
                <input type="text" id="trade-symbol" placeholder="Symbol (e.g. AAPL)" class="trade-input" maxlength="10" autocomplete="off"/>
                <button id="quote-btn" class="trade-btn trade-btn-quote" onclick="fetchQuote()">Quote</button>
            </div>
            <div id="quote-result" class="quote-result" style="display:none;"></div>
            <div class="trade-form-row">
                <input type="number" id="trade-qty" placeholder="Qty" class="trade-input trade-input-sm" min="0.01" step="0.01"/>
                <select id="trade-type" class="trade-select">
                    <option value="market">Market</option>
                    <option value="limit">Limit</option>
                </select>
                <input type="number" id="trade-limit-price" placeholder="Limit $" class="trade-input trade-input-sm" min="0.01" step="0.01" style="display:none;"/>
            </div>
            <div class="trade-form-row">
                <button class="trade-btn trade-btn-buy" onclick="placeOrder('buy')">Buy</button>
                <button class="trade-btn trade-btn-sell" onclick="placeOrder('sell')">Sell</button>
            </div>
            <div id="trade-result" class="trade-result" style="display:none;"></div>
        </div>
    </div>
</div>

<!-- ─── Positions ─────────────────────────────────────── -->
<div class="section" id="positions-section" style="display:none;">
    <div class="section-head">
        <div class="section-title">Open Positions</div>
        <div class="section-badge" id="positions-count">0</div>
    </div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>Symbol</th>
                    <th>Qty</th>
                    <th>Avg Entry</th>
                    <th>Current</th>
                    <th>Value</th>
                    <th>P&amp;L</th>
                </tr>
            </thead>
            <tbody id="positions-body">
                <tr><td colspan="6" class="empty-table">No open positions</td></tr>
            </tbody>
        </table>
    </div>
</div>

</div>

<div class="footer">
    StockSignal &mdash; AI predictions for Polymarket stock markets &middot; Auto-refreshes every 60s
    <br><span style="font-size:10px;color:var(--text-muted);">Not investment advice. For informational purposes only. You make your own trading decisions.</span>
</div>

<script>
(function() {{
  let unitSystem = localStorage.getItem('narve_units') || 'american';
  let currencyCode = localStorage.getItem('narve_currency') || 'USD';
  let langCode = localStorage.getItem('narve_language') || 'en';
  function isMetric() {{ return unitSystem === 'european'; }}
  function getLocale() {{ return isMetric() ? 'de-DE' : 'en-US'; }}

  /* ----- i18n ----- */
  const LANGUAGES = [
    ['en','English'],['es','Espa\u00f1ol'],['de','Deutsch'],['fr','Fran\u00e7ais'],
    ['it','Italiano'],['pt','Portugu\u00eas'],['nl','Nederlands'],['pl','Polski'],
    ['ja','\u65e5\u672c\u8a9e'],['ko','\ud55c\uad6d\uc5b4'],['zh','\u4e2d\u6587'],['ru','\u0420\u0443\u0441\u0441\u043a\u0438\u0439'],
    ['hi','\u0939\u093f\u0928\u094d\u0926\u0940'],['ar','\u0627\u0644\u0639\u0631\u0628\u064a\u0629'],['bn','\u09ac\u09be\u0982\u09b2\u09be'],['ur','\u0627\u0631\u062f\u0648'],
    ['id','Bahasa Indonesia'],['tr','T\u00fcrk\u00e7e'],['vi','Ti\u1ebfng Vi\u1ec7t'],['th','\u0e44\u0e17\u0e22'],
  ];
  const I18N = {{
    en: {{'common.loading':'Loading...','common.refresh':'Refresh','common.search':'Search','common.error':'Error','nav.dashboard':'Dashboard','nav.settings':'Settings'}},
    es: {{'common.loading':'Cargando...','common.refresh':'Actualizar','common.search':'Buscar','common.error':'Error','nav.dashboard':'Panel','nav.settings':'Configuraci\u00f3n'}},
    de: {{'common.loading':'Wird geladen...','common.refresh':'Aktualisieren','common.search':'Suchen','common.error':'Fehler','nav.dashboard':'\u00dcbersicht','nav.settings':'Einstellungen'}},
    fr: {{'common.loading':'Chargement...','common.refresh':'Actualiser','common.search':'Rechercher','common.error':'Erreur','nav.dashboard':'Tableau de bord','nav.settings':'Param\u00e8tres'}},
    it: {{'common.loading':'Caricamento...','common.refresh':'Aggiorna','common.search':'Cerca','common.error':'Errore','nav.dashboard':'Pannello','nav.settings':'Impostazioni'}},
    pt: {{'common.loading':'Carregando...','common.refresh':'Atualizar','common.search':'Pesquisar','common.error':'Erro','nav.dashboard':'Painel','nav.settings':'Configura\u00e7\u00f5es'}},
    nl: {{'common.loading':'Laden...','common.refresh':'Vernieuwen','common.search':'Zoeken','common.error':'Fout','nav.dashboard':'Dashboard','nav.settings':'Instellingen'}},
    pl: {{'common.loading':'\u0141adowanie...','common.refresh':'Od\u015bwie\u017c','common.search':'Szukaj','common.error':'B\u0142\u0105d','nav.dashboard':'Panel','nav.settings':'Ustawienia'}},
    ja: {{'common.loading':'\u8aad\u307f\u8fbc\u307f\u4e2d...','common.refresh':'\u66f4\u65b0','common.search':'\u691c\u7d22','common.error':'\u30a8\u30e9\u30fc','nav.dashboard':'\u30c0\u30c3\u30b7\u30e5\u30dc\u30fc\u30c9','nav.settings':'\u8a2d\u5b9a'}},
    ko: {{'common.loading':'\ub85c\ub529 \uc911...','common.refresh':'\uc0c8\ub85c \uace0\uce68','common.search':'\uac80\uc0c9','common.error':'\uc624\ub958','nav.dashboard':'\ub300\uc2dc\ubcf4\ub4dc','nav.settings':'\uc124\uc815'}},
    zh: {{'common.loading':'\u52a0\u8f7d\u4e2d...','common.refresh':'\u5237\u65b0','common.search':'\u641c\u7d22','common.error':'\u9519\u8bef','nav.dashboard':'\u4eea\u8868\u677f','nav.settings':'\u8bbe\u7f6e'}},
    ru: {{'common.loading':'\u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0430...','common.refresh':'\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c','common.search':'\u041f\u043e\u0438\u0441\u043a','common.error':'\u041e\u0448\u0438\u0431\u043a\u0430','nav.dashboard':'\u041f\u0430\u043d\u0435\u043b\u044c','nav.settings':'\u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438'}},
    hi: {{'common.loading':'\u0932\u094b\u0921 \u0939\u094b \u0930\u0939\u093e \u0939\u0948...','common.refresh':'\u0930\u093f\u092b\u093c\u094d\u0930\u0947\u0936 \u0915\u0930\u0947\u0902','common.search':'\u0916\u094b\u091c\u0947\u0902','common.error':'\u0924\u094d\u0930\u0941\u091f\u093f','nav.dashboard':'\u0921\u0948\u0936\u092c\u094b\u0930\u094d\u0921','nav.settings':'\u0938\u0947\u091f\u093f\u0902\u0917\u094d\u0938'}},
    ar: {{'common.loading':'\u062c\u0627\u0631\u064d \u0627\u0644\u062a\u062d\u0645\u064a\u0644...','common.refresh':'\u062a\u062d\u062f\u064a\u062b','common.search':'\u0628\u062d\u062b','common.error':'\u062e\u0637\u0623','nav.dashboard':'\u0644\u0648\u062d\u0629 \u0627\u0644\u0642\u064a\u0627\u062f\u0629','nav.settings':'\u0627\u0644\u0625\u0639\u062f\u0627\u062f\u0627\u062a'}},
    bn: {{'common.loading':'\u09b2\u09cb\u09a1 \u09b9\u099a\u09cd\u099b\u09c7...','common.refresh':'\u09b0\u09bf\u09ab\u09cd\u09b0\u09c7\u09b6 \u0995\u09b0\u09c1\u09a8','common.search':'\u0985\u09a8\u09c1\u09b8\u09a8\u09cd\u09a7\u09be\u09a8','common.error':'\u09a4\u09cd\u09b0\u09c1\u099f\u09bf','nav.dashboard':'\u09a1\u09cd\u09af\u09be\u09b6\u09ac\u09cb\u09b0\u09cd\u09a1','nav.settings':'\u09b8\u09c7\u099f\u09bf\u0982\u09b8'}},
    ur: {{'common.loading':'\u0644\u0648\u0688 \u06c1\u0648 \u0631\u06c1\u0627 \u06c1\u06d2...','common.refresh':'\u0631\u06cc\u0641\u0631\u06cc\u0634 \u06a9\u0631\u06cc\u0646','common.search':'\u062a\u0644\u0627\u0634 \u06a9\u0631\u06cc\u0646','common.error':'\u062e\u0631\u0627\u0628\u06cc','nav.dashboard':'\u0688\u06cc\u0634 \u0628\u0648\u0631\u0688','nav.settings':'\u0633\u06cc\u0679\u0646\u06af\u0632'}},
    id: {{'common.loading':'Memuat...','common.refresh':'Segarkan','common.search':'Cari','common.error':'Kesalahan','nav.dashboard':'Dasbor','nav.settings':'Pengaturan'}},
    tr: {{'common.loading':'Y\u00fckleniyor...','common.refresh':'Yenile','common.search':'Ara','common.error':'Hata','nav.dashboard':'Pano','nav.settings':'Ayarlar'}},
    vi: {{'common.loading':'\u0110ang t\u1ea3i...','common.refresh':'L\u00e0m m\u1edbi','common.search':'T\u00ecm ki\u1ebfm','common.error':'L\u1ed7i','nav.dashboard':'B\u1ea3ng \u0111i\u1ec1u khi\u1ec3n','nav.settings':'C\u00e0i \u0111\u1eb7t'}},
    th: {{'common.loading':'\u0e01\u0e33\u0e25\u0e31\u0e07\u0e42\u0e2b\u0e25\u0e14...','common.refresh':'\u0e23\u0e35\u0e40\u0e1f\u0e23\u0e0a','common.search':'\u0e04\u0e49\u0e19\u0e2b\u0e32','common.error':'\u0e02\u0e49\u0e2d\u0e1c\u0e34\u0e14\u0e1e\u0e25\u0e32\u0e14','nav.dashboard':'\u0e41\u0e14\u0e0a\u0e1a\u0e2d\u0e23\u0e4c\u0e14','nav.settings':'\u0e01\u0e32\u0e23\u0e15\u0e31\u0e49\u0e07\u0e04\u0e48\u0e32'}},
  }};
  function t(key) {{
    const dict = I18N[langCode] || I18N.en;
    return dict[key] || I18N.en[key] || key;
  }}
  function applyTranslations(root) {{
    const scope = root || document;
    scope.querySelectorAll('[data-i18n]').forEach(el => {{
      const v = t(el.getAttribute('data-i18n'));
      if (v) el.textContent = v;
    }});
    scope.querySelectorAll('[data-i18n-placeholder]').forEach(el => {{
      const v = t(el.getAttribute('data-i18n-placeholder'));
      if (v) el.placeholder = v;
    }});
  }}
  window.t = t;
  window.applyNarveTranslations = applyTranslations;
  window.setNarveLanguage = function(code) {{
    if (!I18N[code] || code === langCode) return;
    langCode = code;
    localStorage.setItem('narve_language', code);
    document.documentElement.lang = code;
    applyTranslations();
    const sel = document.getElementById('narve-language-select');
    if (sel) sel.value = code;
  }};

  const CURRENCIES = [
    ['USD','US Dollar'],['EUR','Euro'],['GBP','British Pound'],['JPY','Japanese Yen'],
    ['AUD','Australian Dollar'],['CAD','Canadian Dollar'],['CHF','Swiss Franc'],['CNY','Chinese Yuan'],
    ['HKD','Hong Kong Dollar'],['NZD','New Zealand Dollar'],['SEK','Swedish Krona'],['KRW','South Korean Won'],
    ['SGD','Singapore Dollar'],['NOK','Norwegian Krone'],['MXN','Mexican Peso'],['INR','Indian Rupee'],
    ['ZAR','South African Rand'],['TRY','Turkish Lira'],['BRL','Brazilian Real'],['DKK','Danish Krone'],
    ['PLN','Polish Zloty'],['THB','Thai Baht'],['IDR','Indonesian Rupiah'],['HUF','Hungarian Forint'],
    ['CZK','Czech Koruna'],['ILS','Israeli Shekel'],['PHP','Philippine Peso'],['MYR','Malaysian Ringgit'],
    ['RON','Romanian Leu'],['ISK','Icelandic Krona'],
  ];
  const FX_FALLBACK = {{
    USD:1.0, EUR:0.92, GBP:0.79, JPY:150, AUD:1.52, CAD:1.36, CHF:0.88, CNY:7.20,
    HKD:7.83, NZD:1.65, SEK:10.5, KRW:1340, SGD:1.34, NOK:10.6, MXN:17.0,
    INR:83.0, ZAR:18.5, TRY:32.0, BRL:5.0, DKK:6.85, PLN:3.95, THB:35.0,
    IDR:15700, HUF:360, CZK:23.0, ILS:3.7, PHP:56.0, MYR:4.7, RON:4.6, ISK:137,
  }};
  let _fxRates = FX_FALLBACK;

  function _readFxCache() {{
    try {{ return JSON.parse(localStorage.getItem('narve_fx_rates') || 'null'); }} catch {{ return null; }}
  }}
  function _writeFxCache(rates) {{
    try {{ localStorage.setItem('narve_fx_rates', JSON.stringify({{ rates: rates, fetched_at: Date.now() }})); }} catch {{}}
  }}
  async function ensureFxRates() {{
    const cached = _readFxCache();
    if (cached && cached.rates && Date.now() - cached.fetched_at < 3600000) {{
      _fxRates = cached.rates;
      return _fxRates;
    }}
    try {{
      const r = await fetch('/api/fx-rates', {{ credentials: 'same-origin' }});
      if (r.ok) {{
        const data = await r.json();
        _fxRates = data.rates || FX_FALLBACK;
        _fxRates.USD = 1.0;
        _writeFxCache(_fxRates);
        return _fxRates;
      }}
    }} catch {{}}
    if (cached && cached.rates) {{ _fxRates = cached.rates; }}
    return _fxRates;
  }}
  function getRate(code) {{
    if (!code || code === 'USD') return 1;
    return (_fxRates && _fxRates[code]) || FX_FALLBACK[code] || 1;
  }}
  function getSymbol(code, locale) {{
    try {{
      const parts = new Intl.NumberFormat(locale || getLocale(), {{ style: 'currency', currency: code }}).formatToParts(0);
      const sym = parts.find(p => p.type === 'currency');
      if (sym) return sym.value;
    }} catch {{}}
    return code;
  }}
  function symbolFirst(code, locale) {{
    try {{
      const parts = new Intl.NumberFormat(locale || getLocale(), {{ style: 'currency', currency: code }}).formatToParts(0);
      const cIdx = parts.findIndex(p => p.type === 'currency');
      const nIdx = parts.findIndex(p => p.type === 'integer');
      return cIdx < nIdx;
    }} catch {{ return true; }}
  }}

  function convertCurrencyText(text) {{
    if (!text) return text;
    if (currencyCode === 'USD' && !isMetric()) return text;
    const loc = getLocale();
    const rate = getRate(currencyCode);
    const sym = getSymbol(currencyCode, loc);
    const symFirst = symbolFirst(currencyCode, loc);
    return text.replace(/\\$([+-]?)([\\d,]+(?:\\.\\d+)?)([KMBT]?)/g, function(match, sign, num, suffix) {{
      const value = parseFloat(num.replace(/,/g, ''));
      if (isNaN(value)) return match;
      const decPart = num.includes('.') ? num.split('.')[1] : '';
      const decimals = decPart.length;
      const converted = value * rate;
      const formatted = converted.toLocaleString(loc, {{
        minimumFractionDigits: decimals,
        maximumFractionDigits: Math.max(decimals, suffix ? 1 : 0),
      }});
      return symFirst
        ? sym + sign + formatted + suffix
        : sign + formatted + suffix + ' ' + sym;
    }});
  }}

  function walk(node) {{
    if (node.nodeType === 3) {{
      const newText = convertCurrencyText(node.nodeValue);
      if (newText !== node.nodeValue) node.nodeValue = newText;
    }} else if (node.nodeType === 1) {{
      const tag = node.tagName;
      if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'INPUT' || tag === 'TEXTAREA') return;
      if (node.classList && node.classList.contains('no-unit-convert')) return;
      for (let i = 0; i < node.childNodes.length; i++) walk(node.childNodes[i]);
    }}
  }}

  function applyUnits() {{
    if (currencyCode !== 'USD' || isMetric()) walk(document.body);
    document.querySelectorAll('.narve-unit-btn').forEach(b => {{
      b.classList.toggle('active', b.dataset.unit === unitSystem);
    }});
    const sel = document.getElementById('narve-currency-select');
    if (sel) sel.value = currencyCode;
  }}

  window.setNarveUnits = function(sys) {{
    if (sys === unitSystem) return;
    unitSystem = sys;
    localStorage.setItem('narve_units', sys);
    location.reload();
  }};
  window.setNarveCurrency = function(code) {{
    if (code === currencyCode) return;
    currencyCode = code;
    localStorage.setItem('narve_currency', code);
    location.reload();
  }};

  function injectToggle() {{
    if (document.getElementById('narve-unit-wrap')) return;
    const wrap = document.createElement('div');
    wrap.id = 'narve-unit-wrap';
    wrap.className = 'no-unit-convert';
    wrap.style.cssText = 'position:fixed;top:12px;right:12px;display:flex;gap:4px;z-index:9999;background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:4px;box-shadow:0 2px 8px rgba(0,0,0,0.06);align-items:center;';
    const usBtn = document.createElement('button');
    usBtn.className = 'narve-unit-btn';
    usBtn.dataset.unit = 'american';
    usBtn.title = 'American';
    usBtn.textContent = '\U0001F1FA\U0001F1F8';
    usBtn.style.cssText = 'background:none;border:none;cursor:pointer;padding:4px 8px;font-size:14px;border-radius:4px;color:#6b7280;';
    usBtn.onclick = function() {{ window.setNarveUnits('american'); }};
    const euBtn = document.createElement('button');
    euBtn.className = 'narve-unit-btn';
    euBtn.dataset.unit = 'european';
    euBtn.title = 'European';
    euBtn.textContent = '\U0001F1EA\U0001F1FA';
    euBtn.style.cssText = 'background:none;border:none;cursor:pointer;padding:4px 8px;font-size:14px;border-radius:4px;color:#6b7280;';
    euBtn.onclick = function() {{ window.setNarveUnits('european'); }};
    const langSel = document.createElement('select');
    langSel.id = 'narve-language-select';
    langSel.title = 'Language';
    langSel.style.cssText = 'background:#fff;color:#1f2937;border:1px solid #e5e7eb;border-radius:6px;padding:3px 6px;font-size:11px;cursor:pointer;font-family:inherit;max-width:90px;';
    langSel.innerHTML = LANGUAGES.map(function(l) {{
      return '<option value="' + l[0] + '"' + (l[0] === langCode ? ' selected' : '') + '>' + l[1] + '</option>';
    }}).join('');
    langSel.onchange = function(e) {{ window.setNarveLanguage(e.target.value); }};
    const sel = document.createElement('select');
    sel.id = 'narve-currency-select';
    sel.title = 'Display currency';
    sel.style.cssText = 'background:#fff;color:#1f2937;border:1px solid #e5e7eb;border-radius:6px;padding:3px 6px;font-size:11px;cursor:pointer;font-family:inherit;';
    sel.innerHTML = CURRENCIES.map(function(c) {{
      return '<option value="' + c[0] + '"' + (c[0] === currencyCode ? ' selected' : '') + '>' + c[0] + '</option>';
    }}).join('');
    sel.onchange = function(e) {{ window.setNarveCurrency(e.target.value); }};
    wrap.appendChild(usBtn);
    wrap.appendChild(euBtn);
    wrap.appendChild(langSel);
    wrap.appendChild(sel);
    document.body.appendChild(wrap);
    const style = document.createElement('style');
    style.textContent = '.narve-unit-btn.active {{ background: #10b981 !important; color: #fff !important; }}';
    document.head.appendChild(style);
  }}

  function init() {{
    document.documentElement.lang = langCode;
    injectToggle();
    applyTranslations();
    applyUnits();
    ensureFxRates().then(function() {{
      if (currencyCode !== 'USD' || isMetric()) {{
        const cached = _readFxCache();
        if (cached && Date.now() - cached.fetched_at < 60000) {{
          if (!sessionStorage.getItem('narve_fx_reloaded')) {{
            sessionStorage.setItem('narve_fx_reloaded', '1');
            location.reload();
            return;
          }}
        }}
      }}
      sessionStorage.removeItem('narve_fx_reloaded');
    }});
  }}

  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', init);
  }} else {{
    init();
  }}
}})();
</script>

<script>
/* ── Stock Trading Panel ────────────────────────────── */
(function() {{
  const fmt = (n) => '$' + Number(n).toLocaleString('en-US', {{minimumFractionDigits:2, maximumFractionDigits:2}});

  // Check broker connection on load
  async function checkBroker() {{
    try {{
      const resp = await fetch('/api/trading/credentials');
      if (!resp.ok) return showDisconnected();
      const data = await resp.json();
      if (data.alpaca) {{
        showConnected();
        loadAccount();
        loadPositions();
      }} else {{
        showDisconnected();
      }}
    }} catch(e) {{
      showDisconnected();
    }}
  }}

  function showConnected() {{
    const badge = document.getElementById('broker-status-badge');
    badge.textContent = 'Connected';
    badge.style.color = 'var(--green)';
    document.getElementById('broker-not-connected').style.display = 'none';
    document.getElementById('broker-connected').style.display = 'block';
    document.getElementById('positions-section').style.display = 'block';
  }}

  function showDisconnected() {{
    const badge = document.getElementById('broker-status-badge');
    badge.textContent = 'Not connected';
    badge.style.color = 'var(--text-muted)';
    document.getElementById('broker-not-connected').style.display = 'block';
    document.getElementById('broker-connected').style.display = 'none';
    document.getElementById('positions-section').style.display = 'none';
  }}

  async function loadAccount() {{
    try {{
      const resp = await fetch('/api/trading/stock/account');
      if (!resp.ok) return;
      const data = await resp.json();
      document.getElementById('acct-cash').textContent = fmt(data.cash);
      document.getElementById('acct-portfolio').textContent = fmt(data.portfolio_value);
      document.getElementById('acct-buying-power').textContent = fmt(data.buying_power);
      document.getElementById('acct-mode').textContent = data.paper ? 'Paper' : 'Live';
      document.getElementById('acct-mode').style.color = data.paper ? 'var(--amber)' : 'var(--green)';
    }} catch(e) {{ console.warn('Account load error:', e); }}
  }}

  async function loadPositions() {{
    try {{
      const resp = await fetch('/api/trading/stock/positions');
      if (!resp.ok) return;
      const data = await resp.json();
      const tbody = document.getElementById('positions-body');
      const count = document.getElementById('positions-count');
      if (!data.positions || data.positions.length === 0) {{
        tbody.innerHTML = '<tr><td colspan="6" class="empty-table">No open positions</td></tr>';
        count.textContent = '0';
        return;
      }}
      count.textContent = data.positions.length;
      tbody.innerHTML = data.positions.map(p => {{
        const pnlClass = p.unrealized_pnl >= 0 ? 'profit-text' : 'loss-text';
        const pnlSign = p.unrealized_pnl >= 0 ? '+' : '';
        return `<tr class="trade-row">
          <td><span class="trade-ticker">${{p.symbol}}</span></td>
          <td>${{p.qty}}</td>
          <td>${{fmt(p.avg_entry)}}</td>
          <td>${{fmt(p.current_price)}}</td>
          <td>${{fmt(p.market_value)}}</td>
          <td class="${{pnlClass}}">${{pnlSign}}${{Number(p.unrealized_pnl).toFixed(2)}}</td>
        </tr>`;
      }}).join('');
    }} catch(e) {{ console.warn('Positions load error:', e); }}
  }}

  // Toggle limit price input
  document.getElementById('trade-type').addEventListener('change', function() {{
    document.getElementById('trade-limit-price').style.display =
      this.value === 'limit' ? 'block' : 'none';
  }});

  // Quote
  window.fetchQuote = async function() {{
    const sym = document.getElementById('trade-symbol').value.trim().toUpperCase();
    if (!sym) return;
    const el = document.getElementById('quote-result');
    el.style.display = 'block';
    el.textContent = 'Loading...';
    try {{
      const resp = await fetch('/api/trading/stock/quote?symbol=' + encodeURIComponent(sym));
      const data = await resp.json();
      if (data.error) {{ el.textContent = data.error; return; }}
      el.innerHTML = `<strong>${{data.symbol}}</strong> &mdash; `
        + `Bid: ${{fmt(data.bid)}} &middot; Ask: ${{fmt(data.ask)}} `
        + `<span style="color:var(--text-muted);font-size:11px">`
        + `Spread: ${{fmt(data.ask - data.bid)}}</span>`;
    }} catch(e) {{
      el.textContent = 'Quote failed: ' + e.message;
    }}
  }};

  // Place order
  window.placeOrder = async function(action) {{
    const sym = document.getElementById('trade-symbol').value.trim().toUpperCase();
    const qty = parseFloat(document.getElementById('trade-qty').value);
    const orderType = document.getElementById('trade-type').value;
    const limitPrice = parseFloat(document.getElementById('trade-limit-price').value) || 0;

    if (!sym) {{ alert('Enter a symbol'); return; }}
    if (!qty || qty <= 0) {{ alert('Enter a valid quantity'); return; }}

    const label = action === 'buy' ? 'BUY' : 'SELL';
    const priceLabel = orderType === 'limit' ? ` @ ${{fmt(limitPrice)}}` : ' (market)';
    if (!confirm(`${{label}} ${{qty}} shares of ${{sym}}${{priceLabel}}?`)) return;

    const el = document.getElementById('trade-result');
    el.style.display = 'block';
    el.className = 'trade-result';
    el.textContent = 'Placing order...';

    try {{
      const csrfMeta = document.querySelector('meta[name="csrf-token"]');
      const headers = {{
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
      }};
      if (csrfMeta) headers['X-CSRF-Token'] = csrfMeta.content;

      const resp = await fetch('/api/trading/place', {{
        method: 'POST',
        headers: headers,
        body: JSON.stringify({{
          platform: 'alpaca',
          slug: sym,
          side: action,
          action: action,
          amount: qty,
          price: orderType === 'limit' ? limitPrice : 0,
          question: sym,
        }}),
      }});
      const data = await resp.json();
      if (data.ok || data.status === 'submitted') {{
        el.className = 'trade-result success';
        el.textContent = `Order submitted: ${{label}} ${{qty}} ${{sym}}`;
        loadAccount();
        loadPositions();
      }} else {{
        el.className = 'trade-result error';
        el.textContent = data.error || 'Order failed';
      }}
    }} catch(e) {{
      el.className = 'trade-result error';
      el.textContent = 'Error: ' + e.message;
    }}
  }};

  // Symbol input: uppercase + enter to quote
  document.getElementById('trade-symbol').addEventListener('input', function() {{
    this.value = this.value.toUpperCase();
  }});
  document.getElementById('trade-symbol').addEventListener('keydown', function(e) {{
    if (e.key === 'Enter') {{ e.preventDefault(); fetchQuote(); }}
  }});

  // Init
  checkBroker();
  // Refresh positions every 30s
  setInterval(() => {{
    if (document.getElementById('broker-connected').style.display !== 'none') {{
      loadPositions();
    }}
  }}, 30000);
}})();
</script>
</body>
</html>"""
    return page_html


def build_svg_chart(balances, state=None):
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

    start_bal = state.get("starting_balance", 10000) if isinstance(state, dict) else 10000
    start_y = pad_y + chart_h - ((start_bal - min_val) / val_range) * chart_h
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
        # Authenticate via gateway SSO header (reject if secret not configured unless DEV_MODE)
        # Auth check applies to ALL endpoints (including /api/state)
        if _sso_secret:
            client_secret = self.headers.get("X-Gateway-Secret", "")
            if not hmac.compare_digest(client_secret, _sso_secret):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "Unauthorized"}')
                return
        elif not _DEV_MODE:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "Service misconfigured"}')
            return

        # --- Path routing (all paths are now behind auth) ---
        if self.path == "/" or self.path == "/index.html":
            html = build_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(html.encode())
        elif self.path == "/api/state":
            state = load_state() or {}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(state).encode())
        elif self.path == "/api/fx-rates":
            data = get_fx_rates()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description="StockSignal Dashboard")
    parser.add_argument("--port", type=int, default=DASHBOARD_PORT)
    args = parser.parse_args()

    # Never bind to all interfaces when PRODUCTION=1, even if DEV_MODE leaks through
    _is_prod = os.environ.get("PRODUCTION", "").lower() in ("1", "true", "yes", "on")
    bind_host = "127.0.0.1" if _is_prod else ("0.0.0.0" if _DEV_MODE else "127.0.0.1")
    server = HTTPServer((bind_host, args.port), DashboardHandler)
    print(f"StockSignal dashboard running at http://{bind_host}:{args.port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped")
        server.server_close()


if __name__ == "__main__":
    main()

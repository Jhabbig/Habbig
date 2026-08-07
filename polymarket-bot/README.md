# polymarket-bot — 5-minute Up/Down paper-trading bot

## PARKED — do not schedule

**Status (2026-08 audit): parked.** This bot is **paper-only** — it simulates fills
by walking the CLOB order book; it never places real orders and must stay that way.
The audit found the strategy **structurally negative-EV after fees/spread** on the
5-minute Up/Down markets: the edge signals are smaller than the round-trip cost of
crossing the spread at this frequency. Do **not** wire it into `start_dashboards.sh`,
`docker-compose.yml`, a `deploy/` systemd unit, cron, or any other scheduler. Nothing
auto-starts it today (verified 2026-08-07); keep it that way. The code is retained
for reference and offline signal research only.

## What it is

Simulated $100-per-trade paper trading of Polymarket's rolling 5-minute Up/Down
markets for BTC, ETH, SOL, DOGE, XRP and BNB, using Binance momentum, order-book
imbalance, and (optionally) the crypto-dashboard ML predictor as signals.

Manual run (paper mode only): `python3 polymarket_bot.py [--reset]`

State lives next to the script: `poly_trades.json` (ledger) and
`poly_bot_activity.log` (activity log).

# Stripe setup — 30-min checklist

Right now **nobody can subscribe to any dashboard** because no `STRIPE_PRICE_ID_*` env vars are set anywhere on the production box. The Habbig gateway's `subproduct.py` references env vars like `STRIPE_PRICE_ID_SPORTS_MONTHLY`, but `~/.gateway_env`, `~/Polymarket/gateway/.env.production`, and `~/Habbig/gateway/.env` all have zero `STRIPE_*` entries.

This is the fastest revenue unlock. ~30 min on stripe.com + a few env-var lines.

## Step 1 — create products in Stripe (~20 min)

Log into stripe.com → Products → "+ Add product". Create **one product per dashboard** with two prices (monthly + annual). Suggested setup based on current `gateway/config.json`:

| product | monthly | annual |
|---|---|---|
| narve.ai · Sports | $19.99/mo | $199.00/yr |
| narve.ai · Weather | $7.99/mo | $79.00/yr |
| narve.ai · World | $5.99/mo | $59.00/yr |
| narve.ai · Crypto | $9.99/mo | $99.00/yr |
| narve.ai · Midterm | $14.99/mo | $149.00/yr |
| narve.ai · Top Traders | $12.99/mo | $129.00/yr |
| narve.ai · Whale | $17.99/mo | $179.00/yr |
| narve.ai · Voters | $5.99/mo | $59.00/yr |
| narve.ai · Climate | $5.99/mo | $59.00/yr |
| narve.ai · Disasters | $5.99/mo | $59.00/yr |
| **narve.ai Trader (3 dashboards)** | **$99.00/mo** | **$999.00/yr** |
| **narve.ai Pro (all dashboards)** | **$229.00/mo** | **$1,999.00/yr** |

The Trader and Pro tiers already exist in `gateway/server.py` `PLAN_DEFS`. They're the highest-leverage SKUs — most users want 2–3 dashboards (Trader) or all of them (Pro), and bundling drops decision-fatigue. The pricing in `PLAN_DEFS` is already wired into `/billing`, just needs Stripe price IDs to actually charge.

After creating each product, copy the **price ID** (looks like `price_1Abc23dEFgh4567ijklmnop`).

## Step 2 — drop the IDs into `~/.gateway_env`

```bash
ssh julianhabbig@100.69.44.108
cat >> ~/.gateway_env <<'EOF'
# Stripe price IDs — see STRIPE_SETUP.md for the dashboard mapping
STRIPE_PRICE_ID_SPORTS_MONTHLY=price_xxx
STRIPE_PRICE_ID_SPORTS_ANNUAL=price_xxx
STRIPE_PRICE_ID_WEATHER_MONTHLY=price_xxx
STRIPE_PRICE_ID_WEATHER_ANNUAL=price_xxx
STRIPE_PRICE_ID_WORLD_MONTHLY=price_xxx
STRIPE_PRICE_ID_WORLD_ANNUAL=price_xxx
STRIPE_PRICE_ID_CRYPTO_MONTHLY=price_xxx
STRIPE_PRICE_ID_CRYPTO_ANNUAL=price_xxx
STRIPE_PRICE_ID_MIDTERM_MONTHLY=price_xxx
STRIPE_PRICE_ID_MIDTERM_ANNUAL=price_xxx
STRIPE_PRICE_ID_TRADERS_MONTHLY=price_xxx
STRIPE_PRICE_ID_TRADERS_ANNUAL=price_xxx
STRIPE_PRICE_ID_WHALE_MONTHLY=price_xxx
STRIPE_PRICE_ID_WHALE_ANNUAL=price_xxx
STRIPE_PRICE_ID_VOTERS_MONTHLY=price_xxx
STRIPE_PRICE_ID_VOTERS_ANNUAL=price_xxx
STRIPE_PRICE_ID_CLIMATE_MONTHLY=price_xxx
STRIPE_PRICE_ID_CLIMATE_ANNUAL=price_xxx
STRIPE_PRICE_ID_DISASTERS_MONTHLY=price_xxx
STRIPE_PRICE_ID_DISASTERS_ANNUAL=price_xxx
STRIPE_PRICE_ID_TRADER_MONTHLY=price_xxx
STRIPE_PRICE_ID_TRADER_ANNUAL=price_xxx
STRIPE_PRICE_ID_PRO_MONTHLY=price_xxx
STRIPE_PRICE_ID_PRO_ANNUAL=price_xxx
# Stripe webhook signing secret (Settings → Webhooks → narve.ai endpoint)
STRIPE_WEBHOOK_SECRET=whsec_xxx
# Stripe secret key (Settings → API keys → secret)
STRIPE_SECRET_KEY=sk_live_xxx
EOF
sudo systemctl restart polymarket-gateway  # or kill the watchdog gateway pid
```

The layered .env loader I added earlier picks `~/.gateway_env` as priority 1, so all dashboards see the new vars without further config.

## Step 3 — verify

```bash
curl -sI https://sports.narve.ai/  # should still 302 to /gate
# In a browser, log in, click "Upgrade" on a dashboard, you should hit a real Stripe Checkout
```

If checkout 4xx's, the price ID env var is wrong. The gateway logs at `/tmp/gateway.log` will show which env var it tried to read.

## Notes

- The legacy `gateway/config.json` (Polymarket tree) still has `"stripe_price_monthly": null` for `climate` only. That config isn't read in production (Habbig gateway runs production) so it's harmless, but worth setting if you ever revert to Polymarket gateway.
- `whale` and `disasters` were never in the original Stripe lineup — the env vars above include them since the dashboards exist; adjust if you don't want to sell them yet.
- The narve.ai Pro bundle SKU is added in this same PR (`subproduct.py` PRO entry). It needs `STRIPE_PRICE_ID_PRO_MONTHLY/ANNUAL` for the upgrade button to work.

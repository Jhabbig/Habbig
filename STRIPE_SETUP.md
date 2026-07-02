# Stripe setup — 15-min checklist

Pricing is **plan-level only**. Per-dashboard prices are retired — every
dashboard is included with a plan, and the only SKUs are:

| plan | price | Stripe price needed |
|---|---|---|
| **narve.ai Basic** | **$75/mo** | yes — `STRIPE_PRICE_ID_BASIC_MONTHLY` |
| **narve.ai Pro** | **$180/mo** | yes — `STRIPE_PRICE_ID_PRO_MONTHLY` |
| **narve.ai Enterprise** | **negotiable** | no — deals close via `/enquire`, access granted manually |

Plans bill monthly. The plan definitions live in `gateway/server.py`
(`PLANS`); the Stripe price IDs are read from env so prices can be rotated
without a deploy. Until both env vars are set, the upgrade buttons log an
error instead of charging (and with no `STRIPE_SECRET_KEY` at all, billing
runs in placeholder mode — no real payments).

## Step 1 — create two products in Stripe (~10 min)

Log into stripe.com → Products → "+ Add product":

1. **narve.ai Basic** — one recurring monthly price of **$75.00**
2. **narve.ai Pro** — one recurring monthly price of **$180.00**

After creating each, copy the **price ID** (looks like
`price_1Abc23dEFgh4567ijklmnop`).

## Step 2 — drop the IDs into `~/.gateway_env`

```bash
ssh julianhabbig@100.69.44.108
cat >> ~/.gateway_env <<'EOF'
# Stripe plan price IDs — Basic $75/mo, Pro $180/mo (see STRIPE_SETUP.md)
STRIPE_PRICE_ID_BASIC_MONTHLY=price_xxx
STRIPE_PRICE_ID_PRO_MONTHLY=price_xxx
# Stripe webhook signing secret (Settings → Webhooks → narve.ai endpoint)
STRIPE_WEBHOOK_SECRET=whsec_xxx
# Stripe secret key (Settings → API keys → secret)
STRIPE_SECRET_KEY=sk_live_xxx
EOF
sudo systemctl restart polymarket-gateway  # or kill the watchdog gateway pid
```

The layered .env loader picks `~/.gateway_env` as priority 1, so all
dashboards see the new vars without further config.

## Step 3 — verify

```bash
curl -sI https://weather.narve.ai/  # should still 302 to /gate
# In a browser, log in, open /billing, click "Subscribe — $75/mo" on Basic,
# you should hit a real Stripe Checkout for $75.00/month.
```

If checkout 4xx's, the price ID env var is wrong. The gateway logs at
`/tmp/gateway.log` will show which plan it tried to price.

## Enterprise

There is deliberately no Stripe product for Enterprise — pricing is
negotiated per customer. Leads arrive through the `/enquire` form (visible
on `/pricing` and `/billing`). Close the deal off-platform (invoice, bank
transfer, custom Stripe invoice — whatever fits), then grant access from
`/admin` → user → **Grant Free** with all dashboards selected.

## Migration notes

- Old per-dashboard Stripe products/prices ($5.99–$19.99) and the old
  Trader (£49) / Pro (£149) bundle prices are dead. Archive them in Stripe
  so nobody can subscribe through a stale link.
- Existing subscriptions on old prices keep working: the gateway treats
  per-dashboard subs as **legacy** (access unchanged, cancellable from
  /billing, counted separately in admin revenue), and old `trader_*` plan
  records display as Basic.
- The webhook handler is unchanged (`checkout.session.completed` with
  `type=bundle` metadata), so no Stripe webhook reconfiguration is needed.

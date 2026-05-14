# Stripe Go-Live Checklist

Currently: TEST MODE only. Real cards are not charged.

This doc is the single procedure for flipping narve.ai from Stripe test
keys to live keys. Work top-to-bottom — don't skip the pre-flight.
Operational context lives in [RUNBOOK.md](RUNBOOK.md); webhook hardening
details live in `gateway/stripe_webhook_hardening.py`.

## Pre-flight (1-2 days before)

- [ ] Stripe Dashboard: verify business profile fully populated
- [ ] Stripe Dashboard: enable Stripe Tax for EU/UK/US (VAT compliance)
- [ ] Stripe Dashboard: confirm bank account verified for payouts
- [ ] Cloudflare WAF: confirm webhook endpoint in IP allowlist rule
- [ ] Verify SIGNATURE secret rotated for live mode (different from test webhook secret)

## Code changes (one commit)

```python
# gateway/.env on prod box:
STRIPE_LIVE_MODE=true
STRIPE_SECRET_KEY=sk_live_...
STRIPE_PUBLISHABLE_KEY=pk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...  # live mode value
STRIPE_IP_ALLOWLIST_ENFORCE=true
```

Restart uvicorn after editing `~/.gateway_env`. Confirm `/health`
returns 200 and `gateway.log` shows no Stripe auth errors.

## Smoke test (after flip)

1. Subscribe to a £6 subproduct (Polymarket Weather) with Julian's own real card
2. Confirm webhook fires + subscription persisted in DB
3. Cancel the subscription
4. Verify cancellation email arrives within 5 min
5. Verify pro-rata refund issued if applicable
6. Re-subscribe + use the dashboard for 24h
7. Wait for first weekly digest email
8. Confirm Stripe Dashboard payments arrive in bank

## Rollback procedure

If anything breaks:

1. `STRIPE_LIVE_MODE=false` in env
2. Restart uvicorn
3. Refund any test charges via Stripe Dashboard
4. Communicate to users via /status page

## Post-launch

- [ ] Update `/changelog` mentioning paid mode is live
- [ ] Email users on waitlist
- [ ] Announcement on social
- [ ] Monitor /admin/cost-alerts daily for first week
- [ ] Run pip-audit weekly during launch window

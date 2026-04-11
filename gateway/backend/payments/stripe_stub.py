"""
Stripe payment integration — not yet implemented.
This file is a stub. Wire up Stripe when ready.

To implement:
1. Add Stripe price IDs to .env (see .env.example)
2. Implement create_checkout_session()
3. Implement handle_webhook()
4. Connect to /api/billing/checkout route

Price IDs needed:
  STRIPE_PRICE_TRADER_MONTHLY
  STRIPE_PRICE_TRADER_ANNUAL
  STRIPE_PRICE_PRO_MONTHLY
  STRIPE_PRICE_PRO_ANNUAL
  STRIPE_PRICE_TRADING_ADDON_MONTHLY
  STRIPE_PRICE_TRADING_ADDON_ANNUAL
"""


def create_checkout_session(*args, **kwargs):
    raise NotImplementedError("Stripe not yet configured. See stripe_stub.py")


def handle_webhook(*args, **kwargs):
    raise NotImplementedError("Stripe not yet configured. See stripe_stub.py")


def create_portal_session(*args, **kwargs):
    raise NotImplementedError("Stripe not yet configured. See stripe_stub.py")

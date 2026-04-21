"""
Stripe payment integration — NOT YET IMPLEMENTED. DO NOT ENABLE IN
PRODUCTION WITHOUT WEBHOOK SIGNATURE VERIFICATION.

!!! SECURITY WARNING (M9) !!!
An unauthenticated Stripe webhook endpoint is a subscription-forgery
vulnerability: any attacker who knows the URL can POST a fake
``checkout.session.completed`` event and mint a paid subscription.
``handle_webhook`` MUST verify the Stripe signature header with
``stripe.Webhook.construct_event(raw_body, sig_header, STRIPE_WEBHOOK_SECRET)``
BEFORE reading the payload or touching any user / subscription state.
On ``stripe.error.SignatureVerificationError`` the handler must return
HTTP 400 without side-effects.

This module is a stub; the real implementation is deliberately absent
so that an accidental route mount cannot silently accept forged events.

To implement:
1. Add Stripe price IDs to .env (see .env.example)
2. Implement create_checkout_session()
3. Implement handle_webhook() with signature verification (see below)
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
    raise NotImplementedError(
        "Stripe webhook signature verification required. "
        "Use stripe.Webhook.construct_event(raw_body, sig_header, STRIPE_WEBHOOK_SECRET) "
        "and reject 400 on SignatureVerificationError before touching any state."
    )


def create_portal_session(*args, **kwargs):
    raise NotImplementedError("Stripe not yet configured. See stripe_stub.py")

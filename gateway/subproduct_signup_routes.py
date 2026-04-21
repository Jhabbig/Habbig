"""Subproduct-scoped signup + Stripe Checkout routes.

Separate from the main auth flow so the sub-brand landing pages can
ship a single-product checkout experience (one tagline, one price,
one CTA) without pulling the user through the full narve.ai Pro
onboarding.

Flow:

  1. Visitor hits <slug>.narve.ai — landing page served by middleware.
  2. Visitor clicks "Get <Product>" → POST /subproduct-signup with
     email + slug. We create a shell user row + a magic-link token,
     then 302 to Stripe Checkout for the subproduct price.
  3. Stripe Checkout succeeds → ``customer.subscription.created``
     webhook sees ``metadata.subproduct_slug`` and writes into
     ``users.subproduct_subscriptions``.
  4. Checkout redirects to /onboarding?subproduct=<slug> on our side,
     which (next step: auth exchange) logs the user in via the email
     magic link and lands on the subproduct dashboard.

Uses the existing ``register(app)`` pattern so server.py just has to
import + call — no route code in server.py.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Optional

from fastapi import Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse


log = logging.getLogger("subproduct.signup")


def _app_url() -> str:
    return os.environ.get("APP_URL", "https://narve.ai").rstrip("/")


def _stripe_price_id(slug: str) -> Optional[str]:
    """Look up the env-configured Stripe price for ``slug``.

    Catalogue lives in gateway/subproduct.py; each entry names the env
    var that holds its monthly price ID. This lets staging point at
    test-mode prices without a code change.
    """
    try:
        from subproduct import SUBPRODUCTS
    except Exception:
        return None
    cfg = SUBPRODUCTS.get(slug)
    if not cfg:
        return None
    env_name = cfg.get("env_price_id")
    if not env_name:
        return None
    val = os.environ.get(env_name, "").strip()
    return val or None


def _create_or_get_shell_user(email: str) -> int:
    """Idempotent: create a pending user if one doesn't exist, return id.

    We never store a password here — the Stripe-success redirect kicks
    off the email magic-link auth flow. The shell row exists so the
    subproduct_subscriptions JSON has somewhere to land when the
    Stripe webhook fires.
    """
    import db  # local import — matches the project convention
    email = email.strip().lower()
    now = int(time.time())

    with db.conn() as c:
        row = c.execute(
            "SELECT id FROM users WHERE email = ?", (email,),
        ).fetchone()
        if row:
            return int(row["id"])

        # Derive a unique username from the email local part; admin
        # can rename later. The password fields are empty strings —
        # this user cannot log in with a password until they go
        # through the magic-link flow.
        username_base = email.split("@", 1)[0][:30] or f"user_{now}"
        username = username_base
        suffix = 0
        while True:
            exists = c.execute(
                "SELECT 1 FROM users WHERE username = ?", (username,),
            ).fetchone()
            if not exists:
                break
            suffix += 1
            username = f"{username_base}{suffix}"

        cur = c.execute(
            "INSERT INTO users (username, email, password_hash, "
            "password_salt, created_at, is_admin, subproduct_subscriptions) "
            "VALUES (?, ?, '', '', ?, 0, '{}')",
            (username, email, now),
        )
        return int(cur.lastrowid)


def _build_checkout_session(
    *, email: str, price_id: str, slug: str, user_id: int,
) -> str:
    """Create a Stripe Checkout session and return its hosted URL."""
    import stripe  # type: ignore[import]
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

    app_url = _app_url()
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=email,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{app_url}/onboarding?subproduct={slug}&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"https://{slug}.narve.ai/?checkout_cancelled=1",
        metadata={
            "user_id": str(user_id),
            "subproduct_slug": slug,
            # Kept for the webhook's compatibility with the existing
            # narve.ai Pro flow — lets one handler branch on the
            # presence of subproduct_slug.
            "flow": "subproduct",
        },
        subscription_data={"metadata": {
            "user_id": str(user_id),
            "subproduct_slug": slug,
        }},
    )
    return str(session.url)


def register(app) -> None:
    """Attach routes. Called from server.py; no business logic lives there."""

    @app.post("/api/billing/subproduct-checkout")
    async def api_subproduct_checkout(request: Request):
        """Programmatic checkout creator. Accepts JSON body with
        ``email`` + ``subproduct``. Returns the Stripe Checkout URL
        for the SPA to redirect to."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        email = (body.get("email") or "").strip().lower()
        slug = (body.get("subproduct") or "").strip()
        if not email or "@" not in email:
            return JSONResponse({"error": "Valid email required"}, status_code=400)
        price_id = _stripe_price_id(slug)
        if not price_id:
            return JSONResponse(
                {"error": f"Subproduct {slug!r} not configured"},
                status_code=400,
            )
        try:
            user_id = _create_or_get_shell_user(email)
            url = _build_checkout_session(
                email=email, price_id=price_id, slug=slug, user_id=user_id,
            )
        except HTTPException:
            raise
        except Exception as exc:
            log.exception("subproduct checkout failed: %s", exc)
            return JSONResponse(
                {"error": "Checkout temporarily unavailable"},
                status_code=502,
            )
        return JSONResponse({"checkout_url": url})

    @app.post("/subproduct-signup")
    async def subproduct_signup(
        request: Request,
        email: str = Form(""),
        subproduct: str = Form(""),
    ):
        """Form-post entry point used by the landing page CTA.

        Same logic as the JSON route above but returns a 302 directly
        so the button can be a plain ``<form method=post>`` with no JS.
        Preserves the subproduct attached by SubproductMiddleware when
        the visitor came in on <slug>.narve.ai — we trust it over the
        form field so a scraped form can't cross-buy.
        """
        attached = getattr(request.state, "subproduct", None)
        slug = (attached or subproduct or "").strip()
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            return RedirectResponse(
                f"https://{slug}.narve.ai/?error=email" if slug else "/",
                status_code=302,
            )
        price_id = _stripe_price_id(slug)
        if not price_id:
            return RedirectResponse(
                f"https://{slug}.narve.ai/?error=config" if slug else "/",
                status_code=302,
            )
        try:
            user_id = _create_or_get_shell_user(email)
            url = _build_checkout_session(
                email=email, price_id=price_id, slug=slug, user_id=user_id,
            )
        except Exception as exc:
            log.exception("subproduct signup failed: %s", exc)
            return RedirectResponse(
                f"https://{slug}.narve.ai/?error=checkout",
                status_code=302,
            )
        return RedirectResponse(url, status_code=302)

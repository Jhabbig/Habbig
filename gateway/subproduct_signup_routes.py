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

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Optional

from fastapi import Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse


log = logging.getLogger("subproduct.signup")


# ── Magic-link auth token ───────────────────────────────────────────────────
#
# A user who completes Stripe Checkout off-platform has no session cookie
# when Stripe redirects them back to ``/onboarding``. The signed token
# below lets the onboarding handler trust the redirect target and mint a
# real session.
#
# Format: ``<user_id>.<jti>.<expires_at>.<hmac>`` (all URL-safe).
# - Signed with ``GATEWAY_COOKIE_SECRET`` (same secret the gate cookie uses).
# - 1-hour TTL — long enough to absorb webhook latency, short enough that
#   a leaked Stripe success URL can't be replayed days later.
# - Single-use: the ``jti`` is burned via the gateway-wide rate-limit
#   store after first redemption, so refresh-back attacks can't re-exchange.

MAGIC_LINK_TTL_SECONDS = 3600  # 1 hour
_MAGIC_LINK_SEP = "."


def _magic_link_secret() -> bytes:
    """HMAC key for the magic-link auth token.

    Falls back to ``SITE_ACCESS_TOKEN`` when ``GATEWAY_COOKIE_SECRET`` is
    unset. server.py refuses to start in production with no
    ``GATEWAY_COOKIE_SECRET``, so the dev-only fallback can never reach
    production.
    """
    return (
        os.environ.get("GATEWAY_COOKIE_SECRET")
        or os.environ.get("SITE_ACCESS_TOKEN")
        or "dev-subproduct-magic-link-secret"
    ).encode()


def mint_magic_link_token(user_id: int) -> str:
    """Produce a signed, single-use, time-bounded magic-link token."""
    expires_at = int(time.time()) + MAGIC_LINK_TTL_SECONDS
    jti = secrets.token_urlsafe(12)
    payload = f"{int(user_id)}{_MAGIC_LINK_SEP}{jti}{_MAGIC_LINK_SEP}{expires_at}"
    mac = hmac.new(_magic_link_secret(), payload.encode(), hashlib.sha256).digest()
    mac_b64 = base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")
    return f"{payload}{_MAGIC_LINK_SEP}{mac_b64}"


def verify_magic_link_token(token: str) -> Optional[dict]:
    """Return the parsed payload if the token is valid, else None.

    Does NOT consume the jti — the onboarding handler is responsible for
    burning it via ``burn_magic_link_jti`` so we get an audit log entry
    on first redemption.
    """
    if not token or _MAGIC_LINK_SEP not in token:
        return None
    parts = token.split(_MAGIC_LINK_SEP)
    if len(parts) != 4:
        return None
    user_id_str, jti, expires_at_str, mac_b64 = parts
    if (
        not user_id_str.isdigit()
        or not expires_at_str.isdigit()
        or not jti
        or not mac_b64
    ):
        return None
    user_id = int(user_id_str)
    expires_at = int(expires_at_str)
    now = int(time.time())
    if expires_at <= now:
        return None
    if expires_at > now + MAGIC_LINK_TTL_SECONDS + 60:
        # Server-side TTL guard — even if an attacker controlled the
        # signing key (they shouldn't), the token can't outlive the cap.
        return None
    payload = f"{user_id}{_MAGIC_LINK_SEP}{jti}{_MAGIC_LINK_SEP}{expires_at}"
    expected_mac = hmac.new(
        _magic_link_secret(), payload.encode(), hashlib.sha256,
    ).digest()
    expected_b64 = base64.urlsafe_b64encode(expected_mac).rstrip(b"=").decode("ascii")
    if not hmac.compare_digest(expected_b64, mac_b64):
        return None
    return {"user_id": user_id, "jti": jti, "expires_at": expires_at}


def burn_magic_link_jti(jti: str) -> bool:
    """Mark ``jti`` as consumed. Returns True if it was already burnt.

    Uses the gateway-wide rate-limit store as a single-use cache — keys
    survive the TTL window in Redis (or the in-memory fallback). Calling
    ``_is_rate_limited(key, 1, ttl)`` returns False the first time and
    True for every subsequent call within the window, which matches the
    Stripe-event idempotency pattern used elsewhere.
    """
    try:
        import server  # local import; cycles if at module load time
    except Exception:
        log.warning(
            "burn_magic_link_jti: server unavailable, skipping idempotency"
        )
        return False
    return server._is_rate_limited(
        f"magic-link-jti:{jti}", 1, MAGIC_LINK_TTL_SECONDS + 120,
    )


def _client_ip(request: Request) -> str:
    """Best-effort client IP for rate-limit keys."""
    try:
        import server
        return server._get_client_ip(request)
    except Exception:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
        return request.client.host if request.client else "unknown"


def _is_production() -> bool:
    return bool(
        os.environ.get("PRODUCTION") or os.environ.get("IS_PRODUCTION"),
    )


def _check_origin(request: Request) -> bool:
    """Origin/Referer apex-match guard for the public form post.

    /subproduct-signup is necessarily exempt from the double-submit CSRF
    cookie because the form is served from <slug>.narve.ai and the POST
    lands on the apex — the cookie can't span the subdomain ↔ apex pair
    in the way the middleware expects. We compensate by requiring the
    Origin/Referer to resolve to narve.ai or a narve.ai subdomain.
    """
    if not _is_production():
        return True
    origin = request.headers.get("origin") or ""
    referer = request.headers.get("referer") or ""
    if not origin and not referer:
        return False
    from urllib.parse import urlparse
    for raw in (origin, referer):
        if not raw:
            continue
        host = (urlparse(raw).hostname or "").lower()
        if not host:
            continue
        if host == "narve.ai" or host.endswith(".narve.ai"):
            return True
    return False


def _mask_email_local(email: str) -> str:
    """Fallback email masker for log lines."""
    try:
        import db
        return db.mask_email(email)
    except Exception:
        if not email or "@" not in email:
            return "(invalid)"
        local, _, domain = email.partition("@")
        return f"{local[:2]}***@{domain}"


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


async def _build_checkout_session(
    *, email: str, price_id: str, slug: str, user_id: int,
) -> str:
    """Create a Stripe Checkout session and return its hosted URL.

    The Stripe SDK call is synchronous and blocks ~150-500ms; we run it
    on a worker thread so the event loop stays free for other requests.
    """
    import stripe  # type: ignore[import]
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

    # Mint a signed, single-use, 1-hour magic-link token. The post-checkout
    # /onboarding handler validates this token and mints a real session
    # cookie so the user who just paid isn't bounced to /token to re-type
    # the same email — see _consume_magic_link in onboarding_routes.py.
    auth_token = mint_magic_link_token(user_id)

    app_url = _app_url()
    session_params = dict(
        mode="subscription",
        customer_email=email,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=(
            f"{app_url}/onboarding"
            f"?subproduct={slug}"
            f"&session_id={{CHECKOUT_SESSION_ID}}"
            f"&auth={auth_token}"
        ),
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
    session = await asyncio.to_thread(
        stripe.checkout.Session.create, **session_params,
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
            url = await _build_checkout_session(
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

        AUDIT 2026-05-15 — this route is in ``_CSRF_EXEMPT_POSTS`` because
        the <form> lives on a subdomain and the POST lands on the apex,
        so the double-submit cookie can't bridge the boundary. We
        compensate with:
          1. Origin/Referer apex-match — blocks cross-site forgery.
          2. Per-IP rate-limit (5/hour) — bounds shell-user spam.
          3. Per-email rate-limit (3/day) — stops one address being
             burned across many IPs.
          4. SUBPRODUCTS slug whitelist — closes the open-redirect
             primitive that interpolating raw ``{slug}`` into a 302
             target otherwise exposes (``subproduct=evil.com#`` ->
             ``https://evil.com#.narve.ai/?error=email``, a redirect to
             attacker-controlled origin because ``#`` truncates the
             netloc).
        """
        try:
            from subproduct import SUBPRODUCTS as _CATALOG
        except Exception:  # pragma: no cover — degraded import
            _CATALOG = {}

        attached = getattr(request.state, "subproduct", None)
        raw_slug = (attached or subproduct or "").strip()
        slug = raw_slug if raw_slug in _CATALOG else ""

        def _safe_error_redirect(reason: str):
            """302 with a slug-safe target. Apex "/" if slug is invalid."""
            if not slug:
                return RedirectResponse("/", status_code=302)
            return RedirectResponse(
                f"https://{slug}.narve.ai/?error={reason}",
                status_code=302,
            )

        email = (email or "").strip().lower()

        # Origin/Referer apex match before any work — cheapest reject.
        if not _check_origin(request):
            log.warning(
                "subproduct-signup: rejected cross-origin POST ip=%s slug=%s",
                _client_ip(request), raw_slug,
            )
            return RedirectResponse("/", status_code=302)

        # Per-IP + per-email rate limits via the gateway-wide rate store.
        try:
            import server as _srv
            ip = _client_ip(request)
            if _srv._is_rate_limited(f"subproduct-signup-ip:{ip}", 5, 3600):
                log.info("subproduct-signup: per-IP cap hit ip=%s", ip)
                if slug:
                    return _safe_error_redirect("rate_limit")
                return RedirectResponse("/", status_code=302)
            if email and _srv._is_rate_limited(
                f"subproduct-signup-email:{email}", 3, 86400,
            ):
                log.info(
                    "subproduct-signup: per-email cap hit email=%s",
                    _mask_email_local(email),
                )
                if slug:
                    return _safe_error_redirect("rate_limit")
                return RedirectResponse("/", status_code=302)
        except Exception:
            # Rate-limit infra down (Redis + in-memory both unreachable)
            # — fail-open rather than blocking legitimate signups; the
            # CSRFMiddleware's per-IP global limit still bounds the
            # blast radius.
            log.exception("subproduct-signup: rate-limit check failed")

        if not slug:
            # Unknown subproduct — refuse outright. Don't leak the
            # attacker's value back into a redirect path.
            return RedirectResponse("/", status_code=302)
        if not email or "@" not in email:
            return _safe_error_redirect("email")
        price_id = _stripe_price_id(slug)
        if not price_id:
            return _safe_error_redirect("config")
        try:
            user_id = _create_or_get_shell_user(email)
            url = await _build_checkout_session(
                email=email, price_id=price_id, slug=slug, user_id=user_id,
            )
        except Exception as exc:
            log.exception("subproduct signup failed: %s", exc)
            return _safe_error_redirect("checkout")
        return RedirectResponse(url, status_code=302)

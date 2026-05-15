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


_DEV_MAGIC_LINK_SECRET = "dev-subproduct-magic-link-secret"


def _magic_link_secret() -> bytes:
    """HMAC key for the magic-link auth token.

    AUDIT #15 MED #2 — the prior implementation fell back to
    ``SITE_ACCESS_TOKEN`` (the public-site gate password) and then to a
    hardcoded dev string. ``SITE_ACCESS_TOKEN`` is shared with every
    operator and rotated rarely; using it to sign a token that mints a
    session cookie on /onboarding gives anyone who has ever seen the
    gate password the ability to forge magic links for any user_id.

    Fix: require a dedicated ``SUBPRODUCT_MAGIC_LINK_SECRET`` env var.
    Dev/test processes fall back to a hardcoded string so the suite
    runs without ceremony — production refuses to start without the
    real secret (see ``_ensure_magic_link_secret_configured`` below,
    wired into ``register()`` so server.py picks the check up at
    startup without us touching server.py).
    """
    secret = os.environ.get("SUBPRODUCT_MAGIC_LINK_SECRET", "").strip()
    if secret:
        return secret.encode()
    if _is_production():
        # Last-ditch defence — the startup guard should have already
        # raised, but if anything reached the signing path in prod
        # without a secret we refuse rather than fall back to a
        # predictable dev string.
        raise RuntimeError(
            "SUBPRODUCT_MAGIC_LINK_SECRET unset in production "
            "(refusing to sign magic-link tokens with dev fallback)"
        )
    return _DEV_MAGIC_LINK_SECRET.encode()


def _ensure_magic_link_secret_configured() -> None:
    """Startup-time guard. Called from ``register(app)`` so server.py
    picks the check up via the existing route-registration import.

    Production with no ``SUBPRODUCT_MAGIC_LINK_SECRET`` → raise.
    Short secret (<32 chars) in production → raise. The startup-time
    failure mirrors the SITE_ACCESS_TOKEN / GATEWAY_COOKIE_SECRET /
    IP_HASH_SALT checks in server.py so an operator deploying with a
    missing env var sees the failure on boot, not when the first
    paying customer hits checkout.
    """
    if not _is_production():
        return
    secret = os.environ.get("SUBPRODUCT_MAGIC_LINK_SECRET", "").strip()
    if not secret:
        log.error(
            "FATAL: PRODUCTION=1 but SUBPRODUCT_MAGIC_LINK_SECRET is unset "
            "— refusing to start (signs magic-link tokens that mint "
            "session cookies on /onboarding)."
        )
        raise RuntimeError(
            "SUBPRODUCT_MAGIC_LINK_SECRET must be set in production "
            "(signs single-use magic-link auth tokens used by the "
            "subproduct Stripe-Checkout success redirect)"
        )
    if len(secret) < 32:
        log.error(
            "FATAL: SUBPRODUCT_MAGIC_LINK_SECRET is too short (%d chars) "
            "— refusing to start.",
            len(secret),
        )
        raise RuntimeError(
            "SUBPRODUCT_MAGIC_LINK_SECRET must be at least 32 characters"
        )


# ── Account-takeover guard ─────────────────────────────────────────────────
#
# AUDIT #15 CRIT #1 — the prior ``_create_or_get_shell_user`` returned the
# existing user_id for ANY email already in ``users``, including emails
# attached to a fully registered account (password_hash + password_salt
# set). The downstream ``_build_checkout_session`` then minted a
# magic-link token for that user_id and embedded it in Stripe's
# ``success_url``. An attacker who knew a victim's email could pay $X,
# complete Stripe Checkout, get redirected to ``/onboarding?auth=<token>``
# bound to the victim's user_id, and walk away with a fresh session
# cookie for the victim's account (admin, API keys, payment methods,
# trading history).
#
# Fix: registered accounts (non-empty password_hash AND non-empty
# password_salt) are refused outright. Shell users (zero-length
# password fields) are reused as before — that's the intended
# pre-registration flow. Unknown emails create a fresh shell row.

class RegisteredUserConflict(Exception):
    """Raised by ``_create_or_get_shell_user`` when the email already
    belongs to a registered account. Routes translate this into a
    user-visible "sign in first" response without ever calling
    ``_build_checkout_session`` (so no magic-link token is minted).
    """

    def __init__(self, user_id: int, masked_email: str) -> None:
        super().__init__(f"email already registered for user_id={user_id}")
        self.user_id = user_id
        self.masked_email = masked_email


def mint_magic_link_token(user_id: int) -> str:
    """Produce a signed, single-use, time-bounded magic-link token."""
    expires_at = int(time.time()) + MAGIC_LINK_TTL_SECONDS
    jti = secrets.token_urlsafe(12)
    payload = f"{int(user_id)}{_MAGIC_LINK_SEP}{jti}{_MAGIC_LINK_SEP}{expires_at}"
    mac = hmac.new(_magic_link_secret(), payload.encode(), hashlib.sha256).digest()
    mac_b64 = base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")
    return f"{payload}{_MAGIC_LINK_SEP}{mac_b64}"


def _jti_from_token(token: str) -> str:
    """Best-effort jti extraction — used by audit-log mint rows so the
    REDEEM audit row can be cross-referenced. Does not verify the
    signature; caller is the mint path which just produced the token.
    """
    try:
        return token.split(_MAGIC_LINK_SEP)[1]
    except (IndexError, AttributeError):
        return ""


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


def burn_magic_link_jti(
    jti: str,
    *,
    user_id: Optional[int] = None,
    request=None,
    reason: str = "onboarding-consume",
) -> bool:
    """Mark ``jti`` as consumed. Returns True if it was already burnt.

    Uses the gateway-wide rate-limit store as a single-use cache — keys
    survive the TTL window in Redis (or the in-memory fallback). Calling
    ``_is_rate_limited(key, 1, ttl)`` returns False the first time and
    True for every subsequent call within the window, which matches the
    Stripe-event idempotency pattern used elsewhere.

    AUDIT #15 MED #1 — also writes a ``magic_link.redeem`` row to
    ``audit_log`` (action MAGIC_LINK_REDEEM) so an admin can correlate
    a /onboarding session-mint to the Stripe Checkout that triggered
    it. Audit fields:
      * ``admin_user_id`` = the user_id the token authenticates as
        (this is a self-action, not an admin action — we reuse the
        admin_* columns because that's the only actor surface the
        audit schema currently exposes).
      * ``target_type`` / ``target_id`` = ``user`` / user_id.
      * ``ip_address`` = request client IP (from headers / .client).
      * ``notes`` = reason + first-vs-replayed.

    ``user_id`` and ``request`` are optional so existing callers that
    only have the jti continue to work — the legacy single-arg call
    site in ``test_subproduct_signup_magic_link.TestSingleUseBurn``
    must not need rewriting.
    """
    try:
        import server  # local import; cycles if at module load time
    except Exception:
        log.warning(
            "burn_magic_link_jti: server unavailable, skipping idempotency"
        )
        return False
    already_burnt = server._is_rate_limited(
        f"magic-link-jti:{jti}", 1, MAGIC_LINK_TTL_SECONDS + 120,
    )
    # Audit log — fire on every redemption attempt (first AND replays)
    # so an admin can detect refresh-back attacks even when the burn
    # short-circuits them. Wrapped in try/except so a logging failure
    # never blocks the security-critical jti burn.
    #
    # If the caller didn't supply user_id, look it up from the matching
    # MINT row so the REDEEM row still references the user. The lookup
    # is best-effort — a missing MINT row (audit table truncated, jti
    # already aged off) still produces a non-empty audit trail.
    resolved_user_id = user_id
    if resolved_user_id is None:
        resolved_user_id = _lookup_mint_user_id(jti)
    try:
        from security import audit as _audit
        _audit.log_action(
            admin_user_id=resolved_user_id,
            admin_email=None,
            action=_audit.AuditAction.MAGIC_LINK_REDEEM,
            target_type="user" if resolved_user_id else None,
            target_id=resolved_user_id,
            target_description=None,
            request=request,
            notes=(
                f"reason={reason}; jti={jti}; "
                f"status={'replayed' if already_burnt else 'first_use'}"
            ),
        )
    except Exception as _e:  # pragma: no cover — never block burn
        log.warning("burn_magic_link_jti: audit log_action failed: %s", _e)
    return already_burnt


def _lookup_mint_user_id(jti: str) -> Optional[int]:
    """Reverse-lookup user_id from the most recent MINT audit row.

    Used by ``burn_magic_link_jti`` when the caller (currently
    ``onboarding_routes._consume_magic_link``) only has the jti and not
    the verified user_id. Lets the REDEEM audit row still join back to
    a specific user without forcing the caller signature to change.
    Returns None on any failure — the audit row will still write,
    just with no admin_user_id.
    """
    if not jti:
        return None
    try:
        import db
        with db.conn() as c:
            row = c.execute(
                "SELECT admin_user_id FROM audit_log "
                "WHERE action = ? AND notes LIKE ? "
                "ORDER BY id DESC LIMIT 1",
                ("magic_link.mint", f"%jti={jti}%"),
            ).fetchone()
            if row is None:
                return None
            uid = row["admin_user_id"] if "admin_user_id" in row.keys() else row[0]
            return int(uid) if uid is not None else None
    except Exception:
        return None


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


def _row_is_registered(row) -> bool:
    """A registered user has BOTH password_hash AND password_salt set.

    The shell rows we insert here use empty strings for both columns
    (see the INSERT below); ``queries.auth.create_user`` writes real
    PBKDF2 output and a non-empty salt. We require BOTH fields to be
    non-empty so a half-written row from a crashed signup flow doesn't
    get treated as a registered account and lock the user out.
    """
    try:
        pwd_hash = row["password_hash"]
        pwd_salt = row["password_salt"]
    except (KeyError, IndexError):
        return False
    return bool((pwd_hash or "").strip()) and bool((pwd_salt or "").strip())


def _create_or_get_shell_user(email: str) -> int:
    """Idempotent: create a pending user if one doesn't exist, return id.

    AUDIT #15 CRIT #1 — refuses to return the user_id for an email that
    already belongs to a REGISTERED account (non-empty password_hash
    AND non-empty password_salt). Without this guard the caller would
    mint a magic-link token bound to the victim's user_id and embed it
    in Stripe's success_url, handing whoever completed checkout a
    fresh session cookie for the victim's account on /onboarding.

    Resolution table:
      * email -> registered user → raise RegisteredUserConflict.
        Caller MUST translate this into a user-visible "sign in first"
        response and MUST NOT proceed to ``_build_checkout_session``.
      * email -> shell user (empty password_hash + empty password_salt)
        → reuse the existing id. That's the intended pre-registration
        flow — a customer may abandon checkout and come back later.
      * unknown email → INSERT a new shell row.
    """
    import db  # local import — matches the project convention
    email = email.strip().lower()
    now = int(time.time())

    with db.conn() as c:
        row = c.execute(
            "SELECT id, password_hash, password_salt FROM users "
            "WHERE email = ?",
            (email,),
        ).fetchone()
        if row:
            if _row_is_registered(row):
                raise RegisteredUserConflict(
                    user_id=int(row["id"]),
                    masked_email=_mask_email_local(email),
                )
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
    request=None,
) -> str:
    """Create a Stripe Checkout session and return its hosted URL.

    The Stripe SDK call is synchronous and blocks ~150-500ms; we run it
    on a worker thread so the event loop stays free for other requests.

    AUDIT #15 MED #1 — writes a ``magic_link.mint`` row to ``audit_log``
    before returning, so every magic-link token that ever leaves the
    process has a paper trail tying the mint to the user_id, the
    slug, and the request IP. The audit write is best-effort and
    NEVER blocks checkout — a failure to log must not strand the
    user mid-purchase.
    """
    import stripe  # type: ignore[import]
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

    # Mint a signed, single-use, 1-hour magic-link token. The post-checkout
    # /onboarding handler validates this token and mints a real session
    # cookie so the user who just paid isn't bounced to /token to re-type
    # the same email — see _consume_magic_link in onboarding_routes.py.
    auth_token = mint_magic_link_token(user_id)

    # Audit log the mint. Fire BEFORE Stripe so even a failed Stripe
    # call surfaces in the trail (we can correlate "minted but no
    # checkout completed" anomalies). _to_jti pulls the jti out of
    # the signed payload so the redeem-side audit row joins back.
    try:
        from security import audit as _audit
        _audit.log_action(
            admin_user_id=user_id,
            admin_email=email,
            action=_audit.AuditAction.MAGIC_LINK_MINT,
            target_type="user",
            target_id=user_id,
            target_description=_mask_email_local(email),
            request=request,
            notes=(
                f"reason=subproduct_checkout; slug={slug}; "
                f"jti={_jti_from_token(auth_token)}"
            ),
        )
    except Exception as _e:  # pragma: no cover — never block checkout
        log.warning(
            "subproduct checkout: audit log_action MINT failed: %s", _e,
        )

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

    # Audit #15 MED #2 startup guard. Lives here (not in server.py) so
    # the file-surface for this CRIT fix stays contained — register() is
    # called from server.py before the worker accepts traffic.
    _ensure_magic_link_secret_configured()

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
        except RegisteredUserConflict as conflict:
            # AUDIT #15 CRIT #1 — refuse to mint a magic link for an email
            # already attached to a registered account. Returning 409
            # here (before ``_build_checkout_session``) means we never
            # touch Stripe and never embed a victim-bound token in a
            # success_url. The masked email is safe to surface; the
            # user_id is NOT — leaking it would help account-enumeration.
            log.info(
                "subproduct-checkout: refused — email already registered "
                "email=%s ip=%s slug=%s",
                conflict.masked_email, _client_ip(request), slug,
            )
            return JSONResponse(
                {
                    "error": "Email already registered. "
                             "Sign in first, then upgrade.",
                    "code": "email_already_registered",
                },
                status_code=409,
            )
        try:
            url = await _build_checkout_session(
                email=email, price_id=price_id, slug=slug,
                user_id=user_id, request=request,
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
        except RegisteredUserConflict as conflict:
            # AUDIT #15 CRIT #1 — registered-account email cannot be
            # used to spin up a checkout session, because doing so
            # would mint a magic-link token bound to the victim's
            # user_id and embed it in Stripe's success_url (full
            # account takeover on /onboarding redirect). Bounce to
            # the per-subproduct landing with the ``already_registered``
            # error code so the landing page can prompt the user to
            # sign in first. No magic-link token is minted. No row
            # is added to ``users``.
            log.info(
                "subproduct-signup: refused — email already registered "
                "email=%s ip=%s slug=%s",
                conflict.masked_email, _client_ip(request), slug,
            )
            return _safe_error_redirect("already_registered")
        try:
            url = await _build_checkout_session(
                email=email, price_id=price_id, slug=slug,
                user_id=user_id, request=request,
            )
        except Exception as exc:
            log.exception("subproduct signup failed: %s", exc)
            return _safe_error_redirect("checkout")
        return RedirectResponse(url, status_code=302)

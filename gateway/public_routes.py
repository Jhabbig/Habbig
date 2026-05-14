"""Public marketing / pre-release routes.

Extracted from server.py. Handles the non-auth public endpoints that sit
in front of the gate: /enquire, /pricing, /subscribe, /support, /suspended,
and the prerelease newsletter signup (/api/newsletter*).

Legal pages (/terms, /privacy, /dpa) and source-profile SEO pages
(/sources/{handle}, /sitemap.xml, /robots.txt) live in server_features.py.
Marketing content pages (/about, /faq, /team, ...) live in seo_routes.py.
Those modules own their surface — don't duplicate routes here.

Every cross-module reference goes through ``_srv()`` to avoid circular
imports at startup. Behaviour is byte-identical to the originals.
"""

from __future__ import annotations

import logging
import os
import sys
from json import JSONDecodeError as _JSONDecodeError

from fastapi import Request
from fastapi.responses import JSONResponse

import db


log = logging.getLogger("gateway.public_routes")


# Newsletter rate-limit knobs — only these routes use them, so they live here.
_NEWSLETTER_RATE_MAX = 5              # per-IP new signups per hour
_NEWSLETTER_RATE_WINDOW = 3600        # 1 hour window
_NEWSLETTER_EMAIL_RATE_MAX = 5        # per-email attempts per day
_NEWSLETTER_EMAIL_RATE_WINDOW = 86400 # 24 hour window
_NEWSLETTER_GLOBAL_MAX = 100          # global signups per hour (alarm threshold)


def _srv():
    """Return the already-imported server module (helpers + constants live there)."""
    return sys.modules.get("server") or sys.modules["__main__"]


# ── /enquire ────────────────────────────────────────────────────────────────


async def enquire_page(request: Request):
    srv = _srv()
    sub = srv.get_subdomain(request)
    if sub:
        return await srv.proxy_request(request, "/enquire")
    return srv.render_page(
        "enquire", request=request,
        breadcrumb=[
            ("narve.ai", "/"),
            ("Enquire", None),
        ],
    )


async def api_enquire(request: Request):
    srv = _srv()
    sub = srv.get_subdomain(request)
    if sub:
        return await srv.proxy_request(request, "/api/enquire")
    ip = srv._get_client_ip(request)
    if srv._is_rate_limited(f"{ip}:enquire", srv._RATE_MAX_ENQUIRE):
        return JSONResponse({"error": "Too many requests"}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    email = (body.get("email") or "").strip().lower()
    job_title = (body.get("job_title") or "").strip()
    message = (body.get("message") or "").strip()

    field_max = srv.FIELD_MAX
    email_re = srv.EMAIL_RE
    if len(email) > field_max["email"] or len(job_title) > field_max["enquiry_name"] or len(message) > field_max["enquiry_message"]:
        return JSONResponse({"error": "One or more fields exceed maximum length"}, status_code=400)
    if not email or not email_re.match(email):
        return JSONResponse({"error": "Please enter a valid email address"}, status_code=400)
    if not job_title:
        return JSONResponse({"error": "Please select your role"}, status_code=400)
    if len(message) < 20:
        return JSONResponse({"error": "Please write at least 20 characters"}, status_code=400)
    if len(message) > 500:
        return JSONResponse({"error": "Message is too long (500 characters max)"}, status_code=400)

    db.create_enquiry(email, job_title, message)
    log.info("New enquiry from %s (%s)", email, job_title)

    # Notification email — enqueued via the job queue so the request
    # returns immediately and failures retry automatically.
    enquiry_email = os.environ.get("ENQUIRY_EMAIL")
    if enquiry_email:
        try:
            from jobs.email_jobs import enqueue_email
            await enqueue_email(
                to=enquiry_email,
                template="enquiry_notification",
                context={
                    "enquiry_email": email,
                    "job_title": job_title,
                    "message": message,
                    "app_url": os.environ.get("APP_URL", "https://narve.ai"),
                },
                tags=["enquiry", "transactional"],
            )
            log.info("Enquiry notification enqueued for %s", enquiry_email)
        except Exception as exc:
            log.error("Failed to enqueue enquiry email: %s", exc)

    return JSONResponse({"success": True})


# ── /pricing /subscribe /support /suspended ────────────────────────────────


async def pricing_page(request: Request):
    srv = _srv()
    sub = srv.get_subdomain(request)
    if sub:
        return await srv.proxy_request(request, "/pricing")
    return srv.render_page("pricing", request=request)


async def subscribe_page(request: Request):
    srv = _srv()
    sub = srv.get_subdomain(request)
    if sub:
        return await srv.proxy_request(request, "/subscribe")
    return srv.render_page("subscribe", request=request)


async def api_subscribe(request: Request):
    srv = _srv()
    sub = srv.get_subdomain(request)
    if sub:
        return await srv.proxy_request(request, "/api/subscribe")
    ip = srv._get_client_ip(request)
    if srv._is_rate_limited(f"{ip}:subscribe", srv._RATE_MAX_SUBSCRIBE):
        return JSONResponse({"error": "Too many requests"}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    email = (body.get("email") or "").strip().lower()
    plan = (body.get("plan") or "").strip()
    interval = (body.get("interval") or "monthly").strip()

    field_max = srv.FIELD_MAX
    email_re = srv.EMAIL_RE
    if len(email) > field_max["email"] or len(plan) > 32 or len(interval) > 16:
        return JSONResponse({"error": "Invalid request"}, status_code=400)
    if not email or not email_re.match(email):
        return JSONResponse({"error": "Please enter a valid email address"}, status_code=400)
    if plan not in ("trader", "pro"):
        return JSONResponse({"error": "Invalid plan"}, status_code=400)
    if interval not in ("monthly", "annual"):
        return JSONResponse({"error": "Invalid interval"}, status_code=400)

    # Generate an invite token for the new subscriber
    token = db.create_invite_token(
        note=f"Subscription: {plan} ({interval})",
        target_email=email,
    )
    log.info("Subscription checkout: %s -> %s (%s), token generated", email, plan, interval)
    return JSONResponse({"token": token})


async def support_page(request: Request):
    srv = _srv()
    sub = srv.get_subdomain(request)
    if sub:
        return await srv.proxy_request(request, "/support")
    return srv.render_page("support", request=request)


async def api_support_ticket(request: Request):
    srv = _srv()
    sub = srv.get_subdomain(request)
    if sub:
        return await srv.proxy_request(request, "/api/support-ticket")
    ip = srv._get_client_ip(request)
    if srv._is_rate_limited(f"{ip}:support", srv._RATE_MAX_SUPPORT):
        return JSONResponse({"error": "Too many requests"}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    email = (body.get("email") or "").strip().lower()
    message = (body.get("message") or "").strip()

    field_max = srv.FIELD_MAX
    email_re = srv.EMAIL_RE
    if len(email) > field_max["email"]:
        return JSONResponse({"error": "Email too long"}, status_code=400)
    if not email or not email_re.match(email):
        return JSONResponse({"error": "Please enter a valid email address"}, status_code=400)
    if len(message) < 10:
        return JSONResponse({"error": "Please write at least 10 characters"}, status_code=400)
    if len(message) > 2000:
        return JSONResponse({"error": "Message is too long (2000 characters max)"}, status_code=400)

    db.create_enquiry(email, "Support Ticket", message)
    log.info("Support ticket from %s", email)
    return JSONResponse({"success": True})


async def suspended_page(request: Request):
    srv = _srv()
    sub = srv.get_subdomain(request)
    if sub:
        return await srv.proxy_request(request, "/suspended")
    return srv.render_page("suspended", request=request)


# ── Newsletter signup (pre-release waitlist) ─────────────────────────────
# Rate limiting is layered:
#   - Per-IP:    5 signups per hour    (prevents a single origin spamming)
#   - Per-email: 5 position-checks per day (the unique index on the email
#                column is the real "you can only sign up once" guard — the
#                per-email rate limit just prevents enumerating positions
#                by repeatedly POSTing different addresses)
#   - Global:    100 signups per hour (soft cap to flag bursts in logs)


async def _read_newsletter_body(request: Request) -> dict:
    """Accept either form-urlencoded OR JSON. The prerelease form posts
    urlencoded (because it's a plain <form> submit rewritten as fetch with
    URLSearchParams), but keep the JSON path so curl tests and API clients
    still work.

    Pulls every field the segmented signup form might submit. Unknown
    keys are ignored downstream; missing keys default at the handler.
    """
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/x-www-form-urlencoded" in ctype or "multipart/form-data" in ctype:
        form = await request.form()
        return {
            "email": form.get("email", ""),
            "ref": form.get("ref", ""),
            "segment": form.get("segment", ""),
            "frequency": form.get("frequency", ""),
            "source": form.get("source", ""),
        }
    # Fallback: treat the body as JSON. request.json() raises on non-JSON.
    try:
        data = await request.json()
        if not isinstance(data, dict):
            return {}
        return data
    except (_JSONDecodeError, ValueError, Exception):
        return {}


# Human-readable labels for the confirmation email. Mirrors the segments
# offered by the prerelease + changelog forms — keep in sync with
# queries.newsletter.VALID_SEGMENTS.
_SEGMENT_LABELS = {
    "all": "Everything — all subproducts",
    "markets": "Just markets — sports, crypto, world",
    "election": "Just election + politics",
    "climate": "Just climate + disasters",
    "intelligence": "Just intelligence — signal-search, weekly digest",
}

_FREQUENCY_LABELS = {
    "weekly": "Weekly digest",
    "monthly": "Monthly summary",
    "daily_spike": "Daily on spikes only",
}


def _build_share_url(request: Request, referral_code: str) -> str:
    """Build the absolute share URL the frontend displays.

    We want the copied link to land on the same environment the visitor
    came from. Priority:
      1. If the request host is `staging.<apex>` (or any known non-apex
         subdomain we serve the landing page from), keep the full host so
         staging testers don't get bounced into production.
      2. Otherwise, fall back to the matching apex from ALLOWED_DOMAINS.
      3. Last resort, use the canonical DOMAIN.
    """
    srv = _srv()
    host = srv._request_host(request)
    apex = srv._request_apex(request)
    if host and apex and host != apex:
        # Preserve explicit subdomains (staging.narve.ai, etc.)
        return f"https://{host}/?ref={referral_code}"
    return f"https://{apex or srv.DOMAIN}/?ref={referral_code}"


async def api_newsletter(request: Request):
    """Segmented newsletter signup with double-opt-in.

    Accepts ``email``, optional ``ref``, optional ``segment`` (one of
    ``VALID_SEGMENTS`` — defaults to 'all'), and optional ``frequency``
    (one of ``VALID_FREQUENCIES`` — defaults to 'weekly').

    The response shape is identical regardless of whether the email
    already exists, whether a confirmation email was actually sent, or
    whether the row was unconfirmed. This is deliberate — the endpoint
    must NEVER reveal whether a given email is already on the list,
    so probes can't enumerate subscribers.

    Confirmation flow:
      * Brand-new + cooldown-elapsed re-signups   → enqueue confirmation email
      * Same-window re-signups                    → silent 200, no email
      * Already-confirmed re-signups              → silent 200, prefs updated

    The frontend reads ``success``, ``position``, ``referral_code``,
    ``share_url``, ``is_new`` — all of which still come back. New fields
    (``segment``, ``frequency``, ``confirmation_pending``) are additive.
    """
    srv = _srv()
    body = await _read_newsletter_body(request)

    email = str(body.get("email") or "").strip().lower()
    ref = str(body.get("ref") or "").strip() or None
    segment = (str(body.get("segment") or "").strip().lower() or "all")
    frequency = (str(body.get("frequency") or "").strip().lower() or "weekly")

    field_max = srv.FIELD_MAX
    email_re = srv.EMAIL_RE

    # Clean validation, never leak DB details.
    if not email or len(email) > field_max["email"] or not email_re.match(email):
        return JSONResponse({"error": "Please enter a valid email address."}, status_code=400)

    # Reject unknown segment / frequency strings up-front so we never write
    # garbage to the DB. Defence-in-depth — queries.newsletter clamps too.
    from queries.newsletter import VALID_SEGMENTS, VALID_FREQUENCIES
    if segment not in VALID_SEGMENTS:
        return JSONResponse({"error": "Invalid segment."}, status_code=400)
    if frequency not in VALID_FREQUENCIES:
        return JSONResponse({"error": "Invalid frequency."}, status_code=400)

    ip = srv._get_client_ip(request)

    # Per-IP rate limit (new signups from the same network).
    # Spec calls for 3/hour on /api/newsletter — tighter than the legacy 5
    # because segmented signups give attackers a wider probe surface.
    _PER_IP_LIMIT = 3
    if srv._is_rate_limited(f"{ip}:newsletter", _PER_IP_LIMIT, _NEWSLETTER_RATE_WINDOW):
        return JSONResponse(
            {"error": "Too many signup attempts from your network. Try again in an hour."},
            status_code=429,
        )

    # Per-email rate limit (prevents enumerating positions by POSTing
    # different addresses repeatedly, and stops a bad actor from using
    # someone else's email to burn their attempts).
    if srv._is_rate_limited(
        f"newsletter_email:{email}", _NEWSLETTER_EMAIL_RATE_MAX, _NEWSLETTER_EMAIL_RATE_WINDOW
    ):
        return JSONResponse(
            {"error": "Too many attempts for this email. Try again tomorrow."},
            status_code=429,
        )

    # Global soft cap — doesn't block, just warns loudly so we can react
    # if someone's running a script against us at scale.
    if srv._is_rate_limited("newsletter_global", _NEWSLETTER_GLOBAL_MAX, _NEWSLETTER_RATE_WINDOW):
        log.warning(
            "newsletter signup global cap hit (>%d/hr) — possible spam run ip=%s",
            _NEWSLETTER_GLOBAL_MAX, ip,
        )

    # Honour an explicit ``source`` field from the form (e.g. the changelog
    # page submits source=changelog-page) so admin reporting can tell which
    # surface produced the signup. Fall back to 'prerelease'.
    source = (str(body.get("source") or "").strip() or "prerelease")[:40]

    try:
        result = db.subscribe_newsletter(
            email,
            source=source,
            referred_by=ref,
            segment=segment,
            frequency=frequency,
        )
    except Exception as exc:
        log.exception("subscribe_newsletter failed for email=%s: %s", db.mask_email(email), exc)
        return JSONResponse({"error": "Could not save your signup. Try again."}, status_code=500)

    # Enqueue confirmation email when the DB layer says one is required.
    # The DB enforces the 24h cooldown; we just react to its decision.
    if result.get("confirmation_required") and result.get("confirmation_token"):
        try:
            from jobs.email_jobs import enqueue_email
            apex = srv._request_apex(request) or srv.DOMAIN
            confirm_url = f"https://{apex}/api/newsletter/confirm?token={result['confirmation_token']}"
            # Even the confirmation email gets a one-click unsubscribe in
            # the footer — CAN-SPAM and GDPR are fine with it, and it
            # gives anyone who got the email by mistake a clean exit.
            from urllib.parse import quote
            unsubscribe_url = f"https://{apex}/api/newsletter/unsubscribe?email={quote(email)}"
            await enqueue_email(
                to=email,
                template="newsletter_confirm",
                context={
                    "confirm_url": confirm_url,
                    "segment_label": _SEGMENT_LABELS.get(segment, segment),
                    "frequency_label": _FREQUENCY_LABELS.get(frequency, frequency),
                    "unsubscribe_url": unsubscribe_url,
                },
                tags=["newsletter", "confirm", segment],
            )
            log.info(
                "newsletter confirmation enqueued ip=%s email=%s segment=%s freq=%s",
                ip, db.mask_email(email), segment, frequency,
            )
        except Exception as exc:
            # Don't fail the user-facing request — the row exists, they can
            # just re-submit after the cooldown expires.
            log.exception("newsletter confirmation enqueue failed: %s", exc)

    share_url = _build_share_url(request, result["referral_code"])
    log.info(
        "newsletter signup ip=%s email=%s position=%d is_new=%s ref=%s seg=%s freq=%s",
        ip, db.mask_email(email), result["position"],
        result["is_new"], result["referred_by"] or "-", segment, frequency,
    )
    return JSONResponse({
        "success": True,
        "is_new": result["is_new"],
        "position": result["position"],
        "referral_code": result["referral_code"],
        "share_url": share_url,
        # Tell the UI to render the "check your email" message. Identical
        # 200 shape either way — we never reveal whether a confirmation
        # email was actually sent on this particular request.
        "confirmation_pending": bool(result.get("confirmation_required")),
        "segment": result.get("segment", segment),
        "frequency": result.get("frequency", frequency),
    })


async def api_newsletter_confirm(request: Request, token: str = ""):
    """Accept a confirmation-token click and flip ``confirmed_at``.

    Returns a self-contained HTML page so the link works from any email
    client, with no JS required. Same success message regardless of
    whether the token is new or was already used — re-clicks shouldn't
    look like errors to a confused user.

    Token-shape failures (bad sig, no matching row) render a generic
    "link expired or invalid" message rather than 404. This mirrors the
    anti-enumeration behaviour of the signup endpoint: an attacker can't
    use this surface to test whether a guessed token exists in the DB.
    """
    from fastapi.responses import HTMLResponse
    srv = _srv()
    token = (token or "").strip()

    # Per-IP rate limit on confirmation clicks — token brute-force attempts
    # would otherwise sail through unchallenged.
    ip = srv._get_client_ip(request)
    if srv._is_rate_limited(f"{ip}:newsletter_confirm", 30, 3600):
        return HTMLResponse(
            "<h1>Too many attempts</h1><p>Try again in an hour.</p>",
            status_code=429,
        )

    result = db.confirm_newsletter(token) if token else None
    apex = srv._request_apex(request) or srv.DOMAIN
    home = f"https://{apex}"

    body = """<!DOCTYPE html><html><head><meta charset='utf-8'>
<title>Subscription confirmed — narve.ai</title>
<link rel='stylesheet' href='/_gateway_static/gateway.css?v=5'>
<style>body{background:var(--bg-base);color:var(--text-primary);display:flex;align-items:center;justify-content:center;min-height:100vh;font-family:var(--font-ui);margin:0;}
.card{max-width:440px;padding:48px 40px;background:var(--bg-surface);border:1px solid var(--border-default);border-radius:12px;text-align:center}
h1{font-family:var(--font-display);font-size:28px;margin:0 0 16px;letter-spacing:-0.02em}
p{color:var(--text-secondary);font-size:14px;line-height:1.6}
a{color:var(--text-primary)}</style></head><body><div class='card'>"""

    if result:
        if result.get("was_already_confirmed"):
            body += "<h1>Already confirmed.</h1><p>You're on the list. Nothing more to do.</p>"
        else:
            body += "<h1>Subscription confirmed.</h1><p>You're on the list. We'll send you the first digest within a week.</p>"
            body += "<p style='font-size:12px;color:#aaa;margin-top:16px'>Every email has a one-click unsubscribe link in the footer.</p>"
        log.info("newsletter confirmed ip=%s email=%s segment=%s",
                 ip, db.mask_email(result["email"]), result.get("segment"))
    else:
        body += "<h1>Link expired or invalid.</h1><p>If you didn't sign up recently, you can safely ignore this. Otherwise, request a fresh confirmation by signing up again.</p>"
        log.info("newsletter confirm failed ip=%s token_present=%s", ip, bool(token))

    body += f"<p style='margin-top:28px'><a href='{home}'>Return to narve.ai</a></p></div></body></html>"
    return HTMLResponse(body)


async def api_newsletter_unsubscribe(request: Request, email: str = ""):
    """One-click unsubscribe for newsletter (waitlist) subscribers.

    Distinct from the authenticated-user unsubscribe at /unsubscribe
    (which flips ``users.email_marketing``). Newsletter rows aren't
    necessarily tied to a user account — they predate signup.

    Always returns 200 with the same message regardless of whether the
    email existed. Anti-enumeration parity with the signup endpoint.
    """
    from fastapi.responses import HTMLResponse
    srv = _srv()
    email = (email or "").strip().lower()

    # Per-IP rate limit so a bot can't iterate the list.
    ip = srv._get_client_ip(request)
    if srv._is_rate_limited(f"{ip}:newsletter_unsub", 20, 3600):
        return HTMLResponse(
            "<h1>Too many attempts</h1><p>Try again in an hour.</p>",
            status_code=429,
        )

    # Best-effort unsubscribe. We don't surface the boolean to the UI —
    # the page is identical either way.
    try:
        db.unsubscribe_newsletter(email)
    except Exception as exc:
        log.exception("unsubscribe_newsletter failed: %s", exc)

    apex = srv._request_apex(request) or srv.DOMAIN
    home = f"https://{apex}"
    body = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>Unsubscribed — narve.ai</title>"
        "<link rel='stylesheet' href='/_gateway_static/gateway.css?v=5'>"
        "<style>body{background:var(--bg-base);color:var(--text-primary);display:flex;"
        "align-items:center;justify-content:center;min-height:100vh;"
        "font-family:var(--font-ui);margin:0}.card{max-width:440px;padding:48px 40px;"
        "background:var(--bg-surface);border:1px solid var(--border-default);"
        "border-radius:12px;text-align:center}h1{font-family:var(--font-display);"
        "font-size:28px;margin:0 0 16px;letter-spacing:-0.02em}p{color:var(--text-secondary);"
        "font-size:14px;line-height:1.6}a{color:var(--text-primary)}</style></head>"
        "<body><div class='card'><h1>Unsubscribed.</h1>"
        "<p>You've been removed from the narve.ai newsletter. "
        "It can take up to 24 hours for any pending sends to clear.</p>"
        f"<p style='margin-top:28px'><a href='{home}'>Return to narve.ai</a></p>"
        "</div></body></html>"
    )
    return HTMLResponse(body)


async def api_newsletter_position(request: Request, email: str = ""):
    """Return the current waitlist position for an existing subscriber.

    Used by the prerelease page when a visitor returns via their own
    share link — we want to show them the current number, not assume
    their browser still has the sessionStorage we set at signup.
    """
    srv = _srv()
    email = (email or "").strip().lower()
    field_max = srv.FIELD_MAX
    email_re = srv.EMAIL_RE
    if not email or len(email) > field_max["email"] or not email_re.match(email):
        return JSONResponse({"error": "Invalid email"}, status_code=400)

    # Same per-email bucket as the signup endpoint so position checks count
    # against the email's daily cap too.
    if srv._is_rate_limited(
        f"newsletter_email:{email}", _NEWSLETTER_EMAIL_RATE_MAX, _NEWSLETTER_EMAIL_RATE_WINDOW
    ):
        return JSONResponse({"error": "Too many attempts. Try again tomorrow."}, status_code=429)

    result = db.get_newsletter_position(email)
    if not result:
        # Don't reveal whether the email exists — return a generic 404 shape.
        return JSONResponse({"error": "Not found"}, status_code=404)

    share_url = _build_share_url(request, result["referral_code"])
    return JSONResponse({
        "success": True,
        "position": result["position"],
        "referral_code": result["referral_code"],
        "share_url": share_url,
    })


def register(app) -> None:
    """Wire public marketing + newsletter routes into the given FastAPI app."""
    from fastapi.responses import HTMLResponse
    app.add_api_route("/enquire", enquire_page, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route("/api/enquire", api_enquire, methods=["POST"])
    app.add_api_route("/pricing", pricing_page, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route("/subscribe", subscribe_page, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route("/api/subscribe", api_subscribe, methods=["POST"])
    app.add_api_route("/support", support_page, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route("/api/support-ticket", api_support_ticket, methods=["POST"])
    app.add_api_route("/suspended", suspended_page, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route("/api/newsletter", api_newsletter, methods=["POST"])
    app.add_api_route("/api/newsletter/position", api_newsletter_position, methods=["GET"])
    # Double-opt-in confirmation. GET so it works from any email client.
    app.add_api_route(
        "/api/newsletter/confirm", api_newsletter_confirm, methods=["GET"],
        response_class=HTMLResponse,
    )
    # One-click newsletter unsubscribe (distinct from the authed-user one
    # at /unsubscribe which targets users.email_marketing).
    app.add_api_route(
        "/api/newsletter/unsubscribe", api_newsletter_unsubscribe, methods=["GET"],
        response_class=HTMLResponse,
    )

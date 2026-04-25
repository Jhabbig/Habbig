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
    still work."""
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/x-www-form-urlencoded" in ctype or "multipart/form-data" in ctype:
        form = await request.form()
        return {
            "email": form.get("email", ""),
            "ref": form.get("ref", ""),
        }
    # Fallback: treat the body as JSON. request.json() raises on non-JSON.
    try:
        data = await request.json()
        if not isinstance(data, dict):
            return {}
        return data
    except (_JSONDecodeError, ValueError, Exception):
        return {}


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
    srv = _srv()
    body = await _read_newsletter_body(request)

    email = str(body.get("email") or "").strip().lower()
    ref = str(body.get("ref") or "").strip() or None

    field_max = srv.FIELD_MAX
    email_re = srv.EMAIL_RE

    # Clean validation, never leak DB details.
    if not email or len(email) > field_max["email"] or not email_re.match(email):
        return JSONResponse({"error": "Please enter a valid email address."}, status_code=400)

    ip = srv._get_client_ip(request)

    # Per-IP rate limit (new signups from the same network).
    if srv._is_rate_limited(f"{ip}:newsletter", _NEWSLETTER_RATE_MAX, _NEWSLETTER_RATE_WINDOW):
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

    try:
        result = db.subscribe_newsletter(email, source="prerelease", referred_by=ref)
    except Exception as exc:
        log.exception("subscribe_newsletter failed for email=%s: %s", db.mask_email(email), exc)
        return JSONResponse({"error": "Could not save your signup. Try again."}, status_code=500)

    share_url = _build_share_url(request, result["referral_code"])
    log.info(
        "newsletter signup ip=%s email=%s position=%d is_new=%s ref=%s",
        ip, db.mask_email(email), result["position"],
        result["is_new"], result["referred_by"] or "-",
    )
    return JSONResponse({
        "success": True,
        "is_new": result["is_new"],
        "position": result["position"],
        "referral_code": result["referral_code"],
        "share_url": share_url,
    })


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

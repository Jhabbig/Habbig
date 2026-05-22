#!/usr/bin/env python3
"""Regulators Dashboard — FastAPI backend.

Routes:
  - GET /                       → index.html
  - GET /api/feed?…             → unified action feed + market matches (filters: days, jurisdiction, source, tag, severity, topic, has_market, q)
  - GET /api/heatmap?weeks=12   → per-week, per-regulator, per-tag aggregation
  - GET /api/topics?days=90     → per-topic counts (drives the topic-filter chip badges)
  - GET /api/markets            → raw Polymarket + Kalshi market list (debug)
  - GET /api/people             → hand-curated personnel watch with term-end days + matched markets
  - GET /api/stance             → per-regulator speech stance ladder (SEC/FCA/ESMA axes)
  - GET /api/diff               → latest-vs-prior speech diff per regulator
  - GET /api/sdn                → OFAC SDN delta — today vs prior snapshot (12h cache)
  - GET /api/hearings           → Senate Banking + House FS confirmation hearings (1h cache)
  - GET /api/courts             → CJEU + UK + SCOTUS financial-relevant case feed (1h cache)
  - GET /api/sources            → Master list of every registered RSS source
  - GET /feed.xml?…             → RSS 2.0 alert feed with the same filter params as /api/feed
  - POST /api/subscribe         → v1.6 — accept email + filter; send confirmation email
  - GET /api/subscribe/confirm  → email-click confirmation handler
  - GET /api/subscribe/unsubscribe → email-click unsubscribe handler
  - POST /api/digest/send_now   → admin-token-gated digest dispatcher; external cron drives schedule
  - GET /healthz                → liveness

Auth: same gateway-SSO pattern as centralbank-dashboard. Set DEV_MODE=1 to
bypass when running locally.
"""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ingestion import (
    confirmation_hearings,
    court_cases,
    digest_subscribers,
    email_send,
    kalshi_client,
    ofac_sdn,
    polymarket_client,
    unified_feed,
)
from ingestion import sources as sources_registry
from analysis import diff as diff_module
from analysis import email_digest
from analysis import heatmap as heatmap_aggr
from analysis import market_match
from analysis import people as people_roster
from analysis import rss_feed
from analysis import stance as stance_analysis
from analysis.topic_keywords import TOPICS, TOPIC_LABELS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="Regulators Dashboard")

HTML_PATH = Path(__file__).parent / "index.html"

_sso_secret = os.environ.get("GATEWAY_SSO_SECRET", "")
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"
_rss_token = os.environ.get("RSS_SHARED_TOKEN", "")
_digest_admin_token = os.environ.get("DIGEST_ADMIN_TOKEN", "")
_public_base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
if not _sso_secret and not _DEV_MODE:
    log.warning("GATEWAY_SSO_SECRET unset and DEV_MODE off — all requests will 503")
if not _rss_token and not _DEV_MODE:
    log.warning("RSS_SHARED_TOKEN unset — /feed.xml will refuse requests in non-DEV_MODE")
if not _digest_admin_token and not _DEV_MODE:
    log.warning("DIGEST_ADMIN_TOKEN unset — /api/digest/send_now will refuse requests in non-DEV_MODE")

# Routes that bypass the gateway-SSO middleware. RSS readers and email-
# click links can't send custom headers, so each of these handlers gates
# itself via its own URL-param token instead.
_SSO_BYPASS = (
    "/healthz",
    "/feed.xml",
    "/api/subscribe",
    "/api/subscribe/confirm",
    "/api/subscribe/unsubscribe",
    "/api/digest/send_now",
)


@app.middleware("http")
async def security_and_auth(request: Request, call_next):
    if request.url.path not in _SSO_BYPASS:
        if _sso_secret:
            client_secret = request.headers.get("x-gateway-secret", "")
            if not hmac.compare_digest(client_secret, _sso_secret):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
        elif not _DEV_MODE:
            return JSONResponse({"error": "Service misconfigured"}, status_code=503)

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    if _sso_secret:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"))


def _apply_item_filters(items: list, *, jurisdiction: str, source: str,
                        tag: str, severity: str, topic: str, q: str) -> list:
    """Shared filter logic for /api/feed and /feed.xml. Pure — no I/O."""
    if jurisdiction:
        wanted = {j.strip().upper() for j in jurisdiction.split(",") if j.strip()}
        items = [it for it in items if it.get("jurisdiction") in wanted]
    if source:
        wanted = {s.strip().upper() for s in source.split(",") if s.strip()}
        items = [it for it in items if it.get("source") in wanted]
    if tag:
        wanted = {t.strip().lower() for t in tag.split(",") if t.strip()}
        def tag_hit(it: dict) -> bool:
            if "other" in wanted and not it.get("tags"):
                return True
            if it.get("primary_tag") in wanted:
                return True
            return any(t in wanted for t in it.get("tags", []))
        items = [it for it in items if tag_hit(it)]
    if severity:
        wanted = {s.strip().lower() for s in severity.split(",") if s.strip()}
        def sev_hit(it: dict) -> bool:
            sev = it.get("severity")
            bucket = sev["bucket"] if sev else "none"
            return bucket in wanted
        items = [it for it in items if sev_hit(it)]
    if topic:
        wanted = {t.strip().lower() for t in topic.split(",") if t.strip()}
        items = [it for it in items if any(t in wanted for t in it.get("topics", []))]
    if q:
        needle = q.lower().strip()
        if needle:
            items = [
                it for it in items
                if needle in it.get("title", "").lower()
                or needle in it.get("summary", "").lower()
            ]
    return items


@app.get("/api/feed")
async def api_feed(
    days: int = 90,
    jurisdiction: str = "",
    source: str = "",
    tag: str = "",
    severity: str = "",
    topic: str = "",
    has_market: bool = False,
    q: str = "",
    force: bool = False,
) -> JSONResponse:
    days = max(1, min(days, 365))
    data = unified_feed.get_cached(force=force, since_days=days)

    items = _apply_item_filters(
        data["items"],
        jurisdiction=jurisdiction, source=source, tag=tag,
        severity=severity, topic=topic, q=q,
    )

    # v0.5: attach market matches per item (5-min market cache, in-memory
    # join). Use a fresh shallow-copy list so the cached unified_feed items
    # aren't mutated across requests.
    poly = polymarket_client.get_cached()
    kal = kalshi_client.get_cached()
    all_markets = poly["markets"] + kal["markets"]
    items = market_match.attach_matches(items, all_markets)

    if has_market:
        items = [it for it in items if it.get("markets")]

    return JSONResponse({
        "fetched_at": data["fetched_at"],
        "since_days": data["since_days"],
        "sources": data["sources"],
        "market_sources": [
            {"name": "polymarket", "ok": poly["ok"], "count": poly["count"], "error": poly["error"]},
            {"name": "kalshi",     "ok": kal["ok"],  "count": kal["count"],  "error": kal["error"]},
        ],
        "items": items,
        "count": len(items),
    })


@app.get("/api/heatmap")
async def api_heatmap(weeks: int = 12, show_empty: bool = False, force: bool = False) -> JSONResponse:
    weeks = max(4, min(weeks, 52))
    data = unified_feed.get_cached(force=force, since_days=max(90, weeks * 7))
    return JSONResponse(heatmap_aggr.aggregate(
        data["items"], data["sources"],
        weeks=weeks, hide_empty=not show_empty,
    ))


@app.get("/api/markets")
async def api_markets(force: bool = False) -> JSONResponse:
    """Raw market list for debugging the join. Returns the combined
    Polymarket + Kalshi normalized markets that the matcher sees."""
    poly = polymarket_client.get_cached(force=force)
    kal = kalshi_client.get_cached(force=force)
    return JSONResponse({
        "polymarket": poly,
        "kalshi": kal,
        "combined_count": poly["count"] + kal["count"],
    })


@app.get("/api/diff")
async def api_diff(force: bool = False) -> JSONResponse:
    """Latest-vs-prior speech diff per regulator. Uses the cached feed —
    no extra fetch cost. v1.2 scope: title + summary only."""
    data = unified_feed.get_cached(force=force)
    return JSONResponse({
        "fetched_at": data["fetched_at"],
        "diffs": diff_module.compute_all(data["items"]),
    })


@app.get("/api/courts")
async def api_courts(force: bool = False) -> JSONResponse:
    """v2.0 — court-case feed across CJEU / UK judiciary / SCOTUS,
    filtered to financial/regulatory relevance."""
    return JSONResponse(court_cases.get_cached(force=force))


@app.get("/api/sources")
async def api_sources() -> JSONResponse:
    """v2.0 — list every registered RSS source. Useful for the UI to
    populate per-source filter chips and document coverage."""
    return JSONResponse({
        "count": len(sources_registry.SOURCES),
        "jurisdictions": sources_registry.jurisdictions(),
        "sources": [
            {"code": s.code, "name": s.name, "jurisdiction": s.jurisdiction,
             "rss_url": s.rss_url}
            for s in sources_registry.SOURCES
        ],
    })


@app.get("/api/hearings")
async def api_hearings(force: bool = False) -> JSONResponse:
    """v1.1 — Senate Banking + House FS confirmation-hearing tracker.
    Per-source try/except + 1h cache; URL drift falls into the
    `ok=false` graceful-degradation lane."""
    return JSONResponse(confirmation_hearings.get_cached(force=force))


@app.get("/feed.xml")
async def feed_rss(
    request: Request,
    days: int = 90,
    jurisdiction: str = "",
    source: str = "",
    tag: str = "",
    severity: str = "",
    topic: str = "",
    q: str = "",
    token: str = "",
) -> Response:
    """v1.5 — RSS 2.0 alert feed. Same filter semantics as /api/feed.
    Subscribe by URL — no per-user subscription state.

    Bypasses the gateway-SSO header check (RSS readers can't supply
    custom headers) and instead gates on `?token=<RSS_SHARED_TOKEN>`
    when that env var is set. In DEV_MODE with no token configured,
    the feed is open."""
    if _rss_token:
        if not hmac.compare_digest(token, _rss_token):
            return Response(content="Unauthorized", status_code=401, media_type="text/plain")
    elif not _DEV_MODE:
        return Response(content="Feed disabled — set RSS_SHARED_TOKEN", status_code=503, media_type="text/plain")

    days = max(1, min(days, 365))
    data = unified_feed.get_cached(since_days=days)
    items = _apply_item_filters(
        data["items"],
        jurisdiction=jurisdiction, source=source, tag=tag,
        severity=severity, topic=topic, q=q,
    )
    # Build a self URL that mirrors what the subscriber requested.
    self_url = str(request.url)
    base_url = f"{request.url.scheme}://{request.url.netloc}/"

    parts: list[str] = ["Filtered regulator action feed"]
    if jurisdiction: parts.append(f"jurisdiction={jurisdiction}")
    if source: parts.append(f"source={source}")
    if tag: parts.append(f"tag={tag}")
    if severity: parts.append(f"severity={severity}")
    if topic: parts.append(f"topic={topic}")
    if q: parts.append(f"q={q}")
    description = " · ".join(parts)

    xml = rss_feed.render(
        items,
        channel_title="Regulators Dashboard — filtered feed",
        channel_description=description,
        channel_link=base_url,
        self_url=self_url,
        limit=50,
    )
    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")


_VALID_FILTER_KEYS = {"jurisdiction", "source", "tag", "severity", "topic", "q"}


def _base_url(request: Request) -> str:
    """Preferred public URL for the dashboard. PUBLIC_BASE_URL wins so
    email links don't expose the internal scheme/port; falls back to the
    request URL's origin for DEV_MODE convenience."""
    if _public_base_url:
        return _public_base_url
    return f"{request.url.scheme}://{request.url.netloc}".rstrip("/")


_EMAIL_OK_RX = __import__("re").compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.post("/api/subscribe")
async def api_subscribe(request: Request) -> JSONResponse:
    """v1.6 — accept an email + filter dict, send a confirmation email,
    return a pending-row reference. Double-opt-in: no further mail goes
    out until the user clicks the link in the confirmation email."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "body must be a JSON object"}, status_code=400)
    email = (body.get("email") or "").strip().lower()
    if not _EMAIL_OK_RX.match(email):
        return JSONResponse({"ok": False, "error": "invalid email"}, status_code=400)
    raw_filter = body.get("filter") or {}
    if not isinstance(raw_filter, dict):
        return JSONResponse({"ok": False, "error": "filter must be an object"}, status_code=400)
    filter_dict = {k: str(v) for k, v in raw_filter.items() if k in _VALID_FILTER_KEYS and v}

    sub = digest_subscribers.add_pending(email, filter_dict)
    base = _base_url(request)
    confirm_url = f"{base}/api/subscribe/confirm?token={sub['confirm_token']}"

    subj, text_body, html_body = email_digest.render_confirmation(
        email=email, confirm_url=confirm_url, filter_dict=filter_dict,
    )
    send_result = email_send.send(
        to_addr=email, subject=subj, html_body=html_body, text_body=text_body,
    )
    log.info("subscribe id=%s email=%s send=%s", sub["id"], email, send_result)
    return JSONResponse({
        "ok": True,
        "id": sub["id"],
        "email": email,
        "filter": filter_dict,
        "status": "pending",
        "email_sent": send_result.get("ok", False),
        "email_dry_run": send_result.get("dry_run", False),
    })


_HTML_PAGE = """<!doctype html><html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#222;max-width:560px;margin:48px auto;padding:0 24px;line-height:1.5"><h2>{title}</h2><p>{body}</p></body></html>"""


@app.get("/api/subscribe/confirm")
async def api_subscribe_confirm(token: str = "") -> Response:
    row = digest_subscribers.confirm(token)
    if not row:
        return Response(
            content=_HTML_PAGE.format(
                title="Link expired or invalid",
                body="This confirmation link is no longer valid. If you still want the digest, sign up again on the dashboard.",
            ),
            status_code=404, media_type="text/html",
        )
    return Response(
        content=_HTML_PAGE.format(
            title="Subscription confirmed",
            body=f"You're now subscribed to the Regulators Dashboard daily digest as <strong>{row['email']}</strong>. The next digest will arrive on the next scheduled dispatch.",
        ),
        media_type="text/html",
    )


@app.get("/api/subscribe/unsubscribe")
async def api_subscribe_unsubscribe(token: str = "") -> Response:
    row = digest_subscribers.unsubscribe(token)
    if not row:
        return Response(
            content=_HTML_PAGE.format(
                title="Link not recognized",
                body="That unsubscribe link doesn't match any subscription we know about.",
            ),
            status_code=404, media_type="text/html",
        )
    return Response(
        content=_HTML_PAGE.format(
            title="Unsubscribed",
            body=f"<strong>{row['email']}</strong> has been unsubscribed. You will receive no further digests.",
        ),
        media_type="text/html",
    )


@app.post("/api/digest/send_now")
async def api_digest_send_now(request: Request, token: str = "") -> JSONResponse:
    """Manually trigger digest dispatch to every confirmed subscriber whose
    last_sent_at is before today UTC. Designed to be hit by an external
    cron / k8s CronJob — separation of concerns means no in-process
    scheduler to babysit.

    Auth: DIGEST_ADMIN_TOKEN required outside DEV_MODE. Pass via the
    `token` query param or the `X-Admin-Token` header."""
    header_token = request.headers.get("x-admin-token", "")
    presented = token or header_token
    if _digest_admin_token:
        if not hmac.compare_digest(presented, _digest_admin_token):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    elif not _DEV_MODE:
        return JSONResponse(
            {"ok": False, "error": "DIGEST_ADMIN_TOKEN not set"},
            status_code=503,
        )

    due = digest_subscribers.list_due()
    feed_data = unified_feed.get_cached()
    base = _base_url(request)
    sent: list[dict] = []
    for sub in due:
        items = _apply_item_filters(
            feed_data["items"],
            jurisdiction=sub["filter"].get("jurisdiction", ""),
            source=sub["filter"].get("source", ""),
            tag=sub["filter"].get("tag", ""),
            severity=sub["filter"].get("severity", ""),
            topic=sub["filter"].get("topic", ""),
            q=sub["filter"].get("q", ""),
        )
        unsubscribe_url = f"{base}/api/subscribe/unsubscribe?token={sub['unsubscribe_token']}"
        subj, text_body, html_body = email_digest.render_daily_digest(
            email=sub["email"], items=items, filter_dict=sub["filter"],
            unsubscribe_url=unsubscribe_url, dashboard_url=base,
        )
        result = email_send.send(
            to_addr=sub["email"], subject=subj,
            html_body=html_body, text_body=text_body,
        )
        if result.get("ok"):
            digest_subscribers.mark_sent(sub["id"])
        sent.append({
            "id": sub["id"], "email": sub["email"],
            "items": len(items),
            "send_ok": result.get("ok", False),
            "dry_run": result.get("dry_run", False),
            "error": result.get("error"),
        })
    return JSONResponse({
        "ok": True,
        "due_count": len(due),
        "sent": sent,
        "stats": digest_subscribers.stats(),
        "dry_run": email_send.is_dry_run(),
    })


@app.get("/api/sdn")
async def api_sdn(force: bool = False) -> JSONResponse:
    """OFAC SDN delta — today's snapshot vs the most-recent prior snapshot.
    Heavy fetch (50MB+ XML) cached 12h. First-snapshot path is honest:
    `delta.first_snapshot=true` rather than implying zero changes."""
    data = ofac_sdn.get_cached(force=force)
    if not data["ok"]:
        return JSONResponse({
            "ok": False,
            "error": data.get("error"),
            "fetched_at": data.get("fetched_at"),
        }, status_code=200)
    today = data["today"]
    delta = data["delta"] or {}
    # Don't ship the full 14k-entry list. Top 20 per side + remainder count
    # is sufficient for the panel; if a power user wants the full list,
    # that's a future endpoint.
    top_n = 20
    added = delta.get("added", []) or []
    removed = delta.get("removed", []) or []
    return JSONResponse({
        "ok": True,
        "fetched_at": data.get("fetched_at"),
        "publish_date": today.get("publish_date"),
        "record_count": today.get("record_count"),
        "delta": {
            "first_snapshot": delta.get("first_snapshot", False),
            "yesterday_publish_date": delta.get("yesterday_publish_date"),
            "added_count": delta.get("added_count", 0),
            "removed_count": delta.get("removed_count", 0),
            "added_preview": added[:top_n],
            "removed_preview": removed[:top_n],
            "added_remainder": max(0, len(added) - top_n),
            "removed_remainder": max(0, len(removed) - top_n),
            "program_deltas": delta.get("program_deltas", []),
        },
    })


@app.get("/api/stance")
async def api_stance(force: bool = False) -> JSONResponse:
    """Per-regulator stance ladder from the most recent speech-tagged item
    for each body. Uses the cached feed — no extra fetch cost."""
    data = unified_feed.get_cached(force=force)
    return JSONResponse({
        "fetched_at": data["fetched_at"],
        "ladder": stance_analysis.compute(data["items"]),
    })


@app.get("/api/people")
async def api_people() -> JSONResponse:
    """Hand-curated personnel watch with days-until-term-end + matched markets.
    Roster source is `data/personnel.py` — edit there to add or refresh entries.
    """
    rows = people_roster.roster()
    poly = polymarket_client.get_cached()
    kal = kalshi_client.get_cached()
    all_markets = poly["markets"] + kal["markets"]
    prepared = market_match.prepare_markets(all_markets)
    for r in rows:
        synthetic = people_roster.synthetic_item_for(r)
        r["markets"] = market_match.match_for_item(synthetic, prepared)
    return JSONResponse({
        "people": rows,
        "market_sources": [
            {"name": "polymarket", "ok": poly["ok"], "count": poly["count"], "error": poly["error"]},
            {"name": "kalshi",     "ok": kal["ok"],  "count": kal["count"],  "error": kal["error"]},
        ],
    })


@app.get("/api/topics")
async def api_topics(days: int = 90, force: bool = False) -> JSONResponse:
    """Per-topic action counts over the cached feed window. Drives the
    topic-filter chip badges; not the individual-item view (that's /api/feed).
    """
    days = max(1, min(days, 365))
    data = unified_feed.get_cached(force=force, since_days=days)
    counts: dict[str, int] = {key: 0 for key in TOPICS.keys()}
    for it in data["items"]:
        for t in it.get("topics", []):
            if t in counts:
                counts[t] += 1
    return JSONResponse({
        "fetched_at": data["fetched_at"],
        "since_days": data["since_days"],
        "total_items": len(data["items"]),
        "topics": [
            {"key": key, "label": TOPIC_LABELS.get(key, key), "count": counts[key]}
            for key in TOPICS.keys()
        ],
    })


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("BIND_HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "7080")))

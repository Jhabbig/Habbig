#!/usr/bin/env python3
"""Voter Pulse Dashboard — FastAPI backend.

Surface:
  GET /            → index.html
  GET /api/summary → mood index + every life indicator + sentiment markets
  GET /api/life    → just the FRED indicators
  GET /api/markets → just the Polymarket sentiment markets
  GET /api/mood    → just the composite mood score
  GET /healthz

Auth: same gateway-SSO pattern as world-state-dashboard / centralbank.
Set DEV_MODE=1 to bypass when running locally.
"""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from analysis import alerts as alerts_analysis
from analysis import clark_fisher as world_analysis
from analysis import elections as election_analysis
from analysis import eras as era_analysis
from analysis import mood_index
from analysis import narrative as narrative_analysis
from analysis import regional_mood as regional_mood_analysis
from analysis import release_feed as release_feed_analysis
from analysis import shareable
from analysis import state_mood as state_mood_analysis
from ingestion import fred_client, polls_client, polymarket_client, regional_cpi_client, states_client, subscribers, worldbank_client

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="Voter Pulse Dashboard")

HTML_PATH = Path(__file__).parent / "index.html"
METHODOLOGY_PATH = Path(__file__).parent / "methodology.html"

# Series we surface in the "by administration" comparison table.
ERA_SERIES = ["CPIAUCSL", "UNRATE", "UMCSENT", "MORTGAGE30US", "GASREGW"]

# Backtest is expensive — monthly sweep from 1978 over many series.
# Cache it for 12h alongside the FRED data.
_BACKTEST_CACHE: dict = {"data": None, "fred_fetched_at": 0.0}

_sso_secret = os.environ.get("GATEWAY_SSO_SECRET", "")
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"
if not _sso_secret and not _DEV_MODE:
    log.warning("GATEWAY_SSO_SECRET unset and DEV_MODE off — all requests will 503")


# Paths that bypass the gateway-SSO middleware. /share/* is intentionally
# public so anyone can unfurl a card on Twitter / Slack / etc; /healthz
# stays public so the container healthcheck can hit it. Email signup +
# unsubscribe are also public — lead-gen surfaces only work if anonymous
# visitors can submit them.
PUBLIC_PATHS = {"/healthz", "/api/subscribe", "/subscribe", "/unsubscribe"}
PUBLIC_PREFIXES = ("/share/",)


@app.middleware("http")
async def security_and_auth(request: Request, call_next):
    path = request.url.path
    is_public = path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)
    if not is_public:
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


@app.get("/methodology", response_class=HTMLResponse)
async def methodology() -> HTMLResponse:
    return HTMLResponse(METHODOLOGY_PATH.read_text(encoding="utf-8"))


@app.get("/api/life")
async def api_life(force: bool = False) -> JSONResponse:
    return JSONResponse(fred_client.get_cached(force=force))


@app.get("/api/markets")
async def api_markets(force: bool = False) -> JSONResponse:
    return JSONResponse(polymarket_client.get_cached(force=force))


@app.get("/api/polls")
async def api_polls(force: bool = False) -> JSONResponse:
    return JSONResponse(polls_client.get_cached(force=force))


@app.get("/api/eras")
async def api_eras(force: bool = False) -> JSONResponse:
    life = fred_client.get_cached(force=force)
    return JSONResponse(era_analysis.compose(life["series"], ERA_SERIES))


def _backtest_payload(force: bool = False) -> dict:
    life = fred_client.get_cached(force=force)
    fetched_at = life.get("fetched_at") or 0.0
    cached = _BACKTEST_CACHE.get("data")
    if cached is not None and _BACKTEST_CACHE.get("fred_fetched_at") == fetched_at and not force:
        return {**cached, "fred_fetched_at": fetched_at, "cached": True}
    payload = election_analysis.run(life["series"])
    _BACKTEST_CACHE["data"] = payload
    _BACKTEST_CACHE["fred_fetched_at"] = fetched_at
    return {**payload, "fred_fetched_at": fetched_at, "cached": False}


@app.get("/api/backtest")
async def api_backtest(force: bool = False) -> JSONResponse:
    return JSONResponse(_backtest_payload(force=force))


@app.get("/api/states")
async def api_states(force: bool = False) -> JSONResponse:
    raw = states_client.get_cached(force=force)
    return JSONResponse({**state_mood_analysis.compose(raw), "fetched_at": raw.get("fetched_at")})


@app.get("/api/world")
async def api_world(force: bool = False) -> JSONResponse:
    raw = worldbank_client.get_cached(force=force)
    return JSONResponse({
        **world_analysis.summarise(raw["countries"]),
        "fetched_at": raw.get("fetched_at"),
    })


@app.get("/api/country/{iso3}")
async def api_country(iso3: str, force: bool = False) -> JSONResponse:
    iso3 = iso3.upper()
    if not (3 <= len(iso3) <= 3 and iso3.isalpha()):
        return JSONResponse({"error": "iso3 must be a 3-letter country code"}, status_code=400)
    profile = worldbank_client.get_country_detail_cached(iso3, force=force)
    profile["trajectory"] = world_analysis.annotate_trajectory(profile.get("trajectory") or [])
    if profile["trajectory"]:
        profile["latest_stage"] = profile["trajectory"][-1]
    return JSONResponse(profile)


def _narrative_payload(force: bool = False) -> dict:
    life = fred_client.get_cached(force=False)
    polls = polls_client.get_cached(force=False)
    composed = mood_index.compose(life["series"])
    composed["label"] = mood_index.label_for(composed["overall"])
    backtest = _backtest_payload(force=False)
    return narrative_analysis.generate(composed, life, polls, backtest, force=force)


@app.get("/api/narrative")
async def api_narrative(force: bool = False) -> JSONResponse:
    return JSONResponse(_narrative_payload(force=force))


@app.get("/api/releases")
async def api_releases(force: bool = False) -> JSONResponse:
    life = fred_client.get_cached(force=force)
    return JSONResponse(release_feed_analysis.compose(life))


def _national_umich(life: dict) -> float | None:
    for s in life.get("series") or []:
        if s.get("series_id") == "UMCSENT" and s.get("latest"):
            return s["latest"]["value"]
    return None


@app.get("/api/regions")
async def api_regions(force: bool = False) -> JSONResponse:
    life = fred_client.get_cached(force=force)
    regional_cpi = regional_cpi_client.get_cached(force=force)
    state_payload = states_client.get_cached(force=force)
    payload = regional_mood_analysis.compose(
        regional_cpi["regions"],
        state_payload,
        _national_umich(life),
    )
    return JSONResponse({**payload, "fetched_at": regional_cpi.get("fetched_at")})


@app.get("/api/mood")
async def api_mood(force: bool = False) -> JSONResponse:
    life = fred_client.get_cached(force=force)
    composed = mood_index.compose(life["series"])
    composed["label"] = mood_index.label_for(composed["overall"])
    return JSONResponse(composed)


@app.get("/api/summary")
async def api_summary(force: bool = False) -> JSONResponse:
    life = fred_client.get_cached(force=force)
    markets = polymarket_client.get_cached(force=force)
    polls = polls_client.get_cached(force=force)
    composed = mood_index.compose(life["series"])
    composed["label"] = mood_index.label_for(composed["overall"])
    eras = era_analysis.compose(life["series"], ERA_SERIES)
    backtest = _backtest_payload(force=force)
    raw_states = states_client.get_cached(force=force)
    states = {**state_mood_analysis.compose(raw_states), "fetched_at": raw_states.get("fetched_at")}
    raw_world = worldbank_client.get_cached(force=force)
    world = {**world_analysis.summarise(raw_world["countries"]), "fetched_at": raw_world.get("fetched_at")}
    narrative = narrative_analysis.generate(composed, life, polls, backtest, force=False)
    releases = release_feed_analysis.compose(life)
    regional_cpi = regional_cpi_client.get_cached(force=force)
    regions = {
        **regional_mood_analysis.compose(regional_cpi["regions"], raw_states, _national_umich(life)),
        "fetched_at": regional_cpi.get("fetched_at"),
    }
    return JSONResponse({
        "mood": composed,
        "life": life,
        "markets": markets,
        "polls": polls,
        "eras": eras,
        "backtest": backtest,
        "states": states,
        "world": world,
        "narrative": narrative,
        "releases": releases,
        "regions": regions,
    })


# ── Shareable cards (public; bypass gateway-SSO) ─────────────────────────────


def _public_base_url(request: Request) -> str:
    """Best-effort canonical base URL for an outbound share link.

    Behind the gateway, `Host` may be `pulse.narve.ai`; we honour the
    proxy-forwarded values when present."""
    fwd_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "pulse.narve.ai"
    fwd_proto = request.headers.get("x-forwarded-proto") or "https"
    return f"{fwd_proto}://{fwd_host}"


def _build_share_payload(kind: str) -> dict:
    """Compute the minimal payload each card needs, without recomputing
    the whole /api/summary."""
    if kind == "mood":
        life = fred_client.get_cached(force=False)
        composed = mood_index.compose(life["series"])
        composed["label"] = mood_index.label_for(composed["overall"])
        return {"mood": composed}
    if kind == "backtest":
        return {"backtest": _backtest_payload(force=False)}
    raise KeyError(kind)


@app.get("/share/mood.png")
async def share_mood_png() -> Response:
    payload = _build_share_payload("mood")
    body, ctype = shareable.render_mood_card(payload["mood"])
    return Response(content=body, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=900"})


@app.get("/share/mood", response_class=HTMLResponse)
async def share_mood(request: Request) -> HTMLResponse:
    base = _public_base_url(request)
    payload = _build_share_payload("mood")
    overall = (payload["mood"] or {}).get("overall")
    label = (payload["mood"] or {}).get("label") or "—"
    big = f"{round(overall)}" if overall is not None else "—"
    desc = f"National mood index: {big} ({label})."
    return HTMLResponse(shareable.html_preview(
        kind="mood",
        og_image_url=f"{base}/share/mood.png",
        title=f"Voter Pulse — mood {big} ({label})",
        description=desc,
        canonical_url=f"{base}/share/mood",
    ))


@app.get("/share/backtest.png")
async def share_backtest_png() -> Response:
    payload = _build_share_payload("backtest")
    body, ctype = shareable.render_backtest_card(payload["backtest"])
    return Response(content=body, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=900"})


@app.get("/share/backtest", response_class=HTMLResponse)
async def share_backtest(request: Request) -> HTMLResponse:
    base = _public_base_url(request)
    payload = _build_share_payload("backtest")
    headline = (payload["backtest"] or {}).get("headline") or {}
    if headline.get("accuracy_pct") is not None:
        desc = (f"The Voter Pulse mood index has called {headline['correct']} of "
                f"{headline['n']} presidential elections ({headline['accuracy_pct']:.0f}%) "
                f"at the {headline['horizon_months']}-month horizon.")
    else:
        desc = "Voter Pulse election backtest."
    return HTMLResponse(shareable.html_preview(
        kind="backtest",
        og_image_url=f"{base}/share/backtest.png",
        title="Voter Pulse — election backtest",
        description=desc,
        canonical_url=f"{base}/share/backtest",
    ))


@app.get("/share/country/{iso3}.png")
async def share_country_png(iso3: str) -> Response:
    iso3 = iso3.upper()
    if not (len(iso3) == 3 and iso3.isalpha()):
        return Response(content=b"bad iso3", status_code=400)
    profile = worldbank_client.get_country_detail_cached(iso3)
    profile["trajectory"] = world_analysis.annotate_trajectory(profile.get("trajectory") or [])
    if profile["trajectory"]:
        profile["latest_stage"] = profile["trajectory"][-1]
    body, ctype = shareable.render_country_card(profile)
    return Response(content=body, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/share/country/{iso3}", response_class=HTMLResponse)
async def share_country(iso3: str, request: Request) -> HTMLResponse:
    iso3 = iso3.upper()
    if not (len(iso3) == 3 and iso3.isalpha()):
        return HTMLResponse("<h1>bad iso3</h1>", status_code=400)
    base = _public_base_url(request)
    profile = worldbank_client.get_country_detail_cached(iso3)
    profile["trajectory"] = world_analysis.annotate_trajectory(profile.get("trajectory") or [])
    name = profile.get("name") or iso3
    latest_stage = (profile["trajectory"][-1] if profile["trajectory"] else {})
    label = latest_stage.get("label") or "—"
    desc = f"{name} on the Clark–Fisher arc: {label}."
    return HTMLResponse(shareable.html_preview(
        kind=f"country/{iso3}",
        og_image_url=f"{base}/share/country/{iso3}.png",
        title=f"{name} — Voter Pulse",
        description=desc,
        canonical_url=f"{base}/share/country/{iso3}",
    ))


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


# ── Email subscriptions (public; bypass SSO) ─────────────────────────────────


@app.post("/api/subscribe")
async def api_subscribe(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected JSON body"}, status_code=400)
    email = (body.get("email") or "").strip()
    if not subscribers.is_valid_email(email):
        return JSONResponse({"error": "invalid email"}, status_code=400)
    result = subscribers.subscribe(email)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@app.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe(request: Request, email: str = "", token: str = "") -> HTMLResponse:
    email = (email or "").strip().lower()
    valid = subscribers.is_valid_email(email) and subscribers.verify_token(email, token)
    if not valid:
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><title>Unsubscribe</title>"
            "<body style='font:14px/1.5 sans-serif;background:#0e1117;color:#e6edf3;"
            "text-align:center;padding:64px;'>"
            "<h1>Link expired or invalid.</h1>"
            "<p style='color:#8b949e'>If you were trying to unsubscribe, please "
            "reply to the last alert email and we'll remove you manually.</p>"
            "</body>",
            status_code=400,
        )
    result = subscribers.unsubscribe(email)
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>Unsubscribed</title>"
        "<body style='font:14px/1.5 sans-serif;background:#0e1117;color:#e6edf3;"
        "text-align:center;padding:64px;'>"
        f"<h1>You're unsubscribed.</h1><p style='color:#8b949e'>{email}</p>"
        "<p><a href='/' style='color:#ec4899;'>Back to the dashboard →</a></p>"
        "</body>"
    )


# ── Admin: alert dispatch (authed; triggered by gateway cron) ────────────────


@app.post("/admin/check-and-send")
async def admin_check_and_send(force: bool = False) -> JSONResponse:
    life = fred_client.get_cached(force=False)
    composed = mood_index.compose(life["series"])
    composed["label"] = mood_index.label_for(composed["overall"])
    polls = polls_client.get_cached(force=False)
    backtest = _backtest_payload(force=False)
    narrative = narrative_analysis.generate(composed, life, polls, backtest, force=False)
    summary = alerts_analysis.check_and_send(
        current_mood=composed.get("overall"),
        narrative_text=(narrative or {}).get("narrative"),
        force=force,
    )
    return JSONResponse(summary)


@app.get("/admin/subscriber-count")
async def admin_subscriber_count() -> JSONResponse:
    return JSONResponse({"count": subscribers.count_active()})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=os.environ.get("BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "7062")),
    )

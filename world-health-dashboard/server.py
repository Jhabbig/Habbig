#!/usr/bin/env python3
"""World Health Dashboard — FastAPI backend (Phase 1).

Phase 1 surface:
  - GET /                       → index.html (3D globe)
  - GET /healthz                → liveness probe
  - GET /api/metrics            → metric catalog (id, name, category, unit, ...)
  - GET /api/countries          → ISO3 / name / region index
  - GET /api/globe/{metric_id}  → {iso3 → latest value} + min/max/quantiles
  - GET /api/country/{iso3}     → all metrics for a country (latest values)
  - GET /api/history/{metric_id}?country=USA → time series
  - GET /api/compare?a=USA&b=DEU → side-by-side country profiles

Auth: gateway-SSO pattern (matches centralbank-dashboard). Set DEV_MODE=1 to
run locally without the shared secret.

Data: WHO Global Health Observatory + World Bank Open Data, 24h disk cache.
"""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from analysis import (
    country_profile,
    disease_atlas,
    drug_supply_chain,
    hai_radar,
    health_edge,
    outbreak_radar,
    treatment_vulnerability,
)
from ingestion import (
    country_codes,
    excess_mortality,
    h5n1_surveillance,
    metrics_catalog,
    outbreak_feeds,
    pheic_tracker,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="World Health Dashboard")

HTML_PATH = Path(__file__).parent / "index.html"

_sso_secret = os.environ.get("GATEWAY_SSO_SECRET", "")
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"
if not _sso_secret and not _DEV_MODE:
    log.warning("GATEWAY_SSO_SECRET unset and DEV_MODE off — all requests will 503")


@app.middleware("http")
async def security_and_auth(request: Request, call_next):
    if request.url.path != "/healthz":
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
    # CSP: globe.gl + three.js + topojson loaded from unpkg/jsdelivr CDNs.
    # connect-src must allow our own origin (default-src 'self' covers it).
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "connect-src 'self' https://unpkg.com https://cdn.jsdelivr.net; "
        "frame-ancestors 'none'"
    )
    if _sso_secret:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"))


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/api/metrics")
async def api_metrics() -> JSONResponse:
    return JSONResponse({"metrics": metrics_catalog.all_metrics()})


@app.get("/api/countries")
async def api_countries() -> JSONResponse:
    return JSONResponse({"countries": country_codes.all_countries()})


@app.get("/api/globe/{metric_id}")
async def api_globe(metric_id: str) -> JSONResponse:
    payload = country_profile.globe_layer(metric_id)
    if "error" in payload:
        return JSONResponse(payload, status_code=404)
    return JSONResponse(payload)


@app.get("/api/country/{iso3}")
async def api_country(iso3: str) -> JSONResponse:
    payload = country_profile.country_profile(iso3)
    if "error" in payload:
        return JSONResponse(payload, status_code=404)
    return JSONResponse(payload)


@app.get("/api/history/{metric_id}")
async def api_history(metric_id: str, country: str = Query(...)) -> JSONResponse:
    payload = country_profile.history(metric_id, country)
    if "error" in payload:
        return JSONResponse(payload, status_code=404)
    return JSONResponse(payload)


@app.get("/api/compare")
async def api_compare(a: str = Query(...), b: str = Query(...)) -> JSONResponse:
    return JSONResponse(country_profile.country_compare(a, b))


# ── Phase 2: outbreaks, PHEIC, H5N1, excess mortality ─────────────────────

@app.get("/api/outbreaks")
async def api_outbreaks(limit: int = 100) -> JSONResponse:
    return JSONResponse(outbreak_radar.radar(limit=limit))


@app.get("/api/outbreaks/by_country/{iso3}")
async def api_outbreaks_country(iso3: str) -> JSONResponse:
    feed = outbreak_feeds.fetch_outbreaks()
    items = outbreak_feeds.by_country(feed).get(iso3.upper(), [])
    return JSONResponse({
        "iso3": iso3.upper(),
        "country": country_codes.name_of(iso3.upper()),
        "items": items,
        "fetched_at": feed.get("fetched_at"),
    })


@app.get("/api/pheic")
async def api_pheic() -> JSONResponse:
    return JSONResponse({
        "active": pheic_tracker.active(),
        "history": pheic_tracker.all_pheics(),
    })


@app.get("/api/h5n1")
async def api_h5n1() -> JSONResponse:
    return JSONResponse(h5n1_surveillance.summary())


@app.get("/api/excess_mortality")
async def api_excess_mortality(country: str | None = Query(None)) -> JSONResponse:
    if country:
        return JSONResponse({
            "iso3": country.upper(),
            "country": country_codes.name_of(country.upper()),
            "points": excess_mortality.country_series(country),
        })
    return JSONResponse(excess_mortality.latest_globe_layer())


# ── Phase 3: prediction markets ────────────────────────────────────────────

@app.get("/api/markets")
async def api_markets() -> JSONResponse:
    return JSONResponse(health_edge.aggregate())


# ── Phase 4b: disease atlas ────────────────────────────────────────────────

@app.get("/api/diseases")
async def api_diseases() -> JSONResponse:
    return JSONResponse({
        "diseases": disease_atlas.list_diseases(),
        "stats": disease_atlas.stats(),
    })


@app.get("/api/disease/{slug}")
async def api_disease(slug: str) -> JSONResponse:
    rec = disease_atlas.get_disease(slug)
    if not rec:
        return JSONResponse({"error": f"unknown slug: {slug}"}, status_code=404)
    return JSONResponse(rec)


# ── Phase 4a: HAI / AMR radar ──────────────────────────────────────────────

@app.get("/api/hai")
async def api_hai() -> JSONResponse:
    return JSONResponse(hai_radar.overview())


@app.get("/api/hai/globe/{indicator_id}")
async def api_hai_globe(indicator_id: str) -> JSONResponse:
    payload = hai_radar.globe_layer(indicator_id)
    if "error" in payload:
        return JSONResponse(payload, status_code=404)
    return JSONResponse(payload)


@app.get("/api/hai/country/{iso3}")
async def api_hai_country(iso3: str) -> JSONResponse:
    payload = hai_radar.country_profile(iso3)
    if "error" in payload:
        return JSONResponse(payload, status_code=404)
    return JSONResponse(payload)


@app.get("/api/hai/c_auris")
async def api_hai_c_auris() -> JSONResponse:
    return JSONResponse(hai_radar.c_auris_summary())


# ── Phase 4c: drug supply chain ────────────────────────────────────────────

@app.get("/api/drug/{name}")
async def api_drug(name: str) -> JSONResponse:
    return JSONResponse(drug_supply_chain.profile(name))


@app.get("/api/shortages")
async def api_shortages() -> JSONResponse:
    return JSONResponse(drug_supply_chain.shortage_overview())


# ── Phase 4d: atlas-wide vulnerability rollup ──────────────────────────────

@app.get("/api/vulnerability")
async def api_vulnerability() -> JSONResponse:
    return JSONResponse(treatment_vulnerability.overview())


@app.get("/api/vulnerability/index")
async def api_vulnerability_index() -> JSONResponse:
    return JSONResponse(treatment_vulnerability.disease_vulnerability_index())


@app.get("/api/vulnerability/disease/{slug}")
async def api_vulnerability_disease(slug: str) -> JSONResponse:
    rec = treatment_vulnerability.disease_vulnerability(slug)
    if not rec:
        return JSONResponse({"error": f"unknown slug: {slug}"}, status_code=404)
    return JSONResponse(rec)


@app.get("/api/shortages/active")
async def api_shortages_active() -> JSONResponse:
    return JSONResponse({
        "items": treatment_vulnerability.active_shortages_with_scores(),
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "7053")))

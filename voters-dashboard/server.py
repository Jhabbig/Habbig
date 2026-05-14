#!/usr/bin/env python3
"""
Voters Dashboard — FastAPI backend.

Slice 1 (Atlas): countries, demographics, issue salience, democracy
indicators, election calendar, and threaded comments + reactions.

Authentication: SSO via gateway-injected `X-Gateway-Secret`,
`X-Gateway-User-Id`, `X-Gateway-User-Email` headers. Mirrors the
pattern used by `world-state-dashboard/`.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import cross_dashboard
import markets
import news

# ──────────────────────────────────────────────────────────────────────────────
# App init
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Voters Dashboard")
log = logging.getLogger("voters")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

ROOT = Path(__file__).parent
HTML_PATH = ROOT / "index.html"
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
COUNTRIES_YAML = DATA_DIR / "countries.yaml"
POLLS_YAML = DATA_DIR / "polls.yaml"
CHAINS_YAML = DATA_DIR / "impact_chains.yaml"
POLITICAL_YAML = DATA_DIR / "political_context.yaml"
INFLUENCES_YAML = DATA_DIR / "voter_influences.yaml"
INFLUENCES_EXTRA_YAML = DATA_DIR / "voter_influences_extra.yaml"
SCHEMA_SQL = ROOT / "schema.sql"
DB_PATH = ROOT / "voters.sqlite"

CACHE_DIR.mkdir(parents=True, exist_ok=True)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ──────────────────────────────────────────────────────────────────────────────
# Auth (gateway SSO)
# ──────────────────────────────────────────────────────────────────────────────

_sso_secret = os.environ.get("GATEWAY_SSO_SECRET", "")
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"

# Reviewer roster: anyone whose authenticated email matches gets reviewer
# powers (chain approve/reject, hide thoughts, etc.). Admins are always
# reviewers. In DEV_MODE the synthetic dev@local user is auto-promoted.
def _parse_email_set(raw: str) -> set[str]:
    return {e.strip().lower() for e in (raw or "").split(",") if e.strip()}


_REVIEWER_EMAILS = _parse_email_set(os.environ.get("VOTERS_REVIEWER_EMAILS", ""))
_ADMIN_EMAILS = _parse_email_set(os.environ.get("VOTERS_ADMIN_EMAILS", ""))


def _user_role(user: dict) -> str:
    email = (user.get("email") or "").lower()
    if email in _ADMIN_EMAILS:
        return "admin"
    if email in _REVIEWER_EMAILS:
        return "reviewer"
    if _DEV_MODE and not _sso_secret and email == "dev@local":
        return "admin"
    return "subscriber"


def _require_reviewer(user: dict) -> None:
    if _user_role(user) not in ("reviewer", "admin"):
        raise HTTPException(status_code=403, detail="reviewer role required")

if not _sso_secret:
    if _DEV_MODE:
        log.warning("GATEWAY_SSO_SECRET not set — voters dashboard running in DEV_MODE (no auth)")
    else:
        log.warning("GATEWAY_SSO_SECRET not set and DEV_MODE not enabled — rejecting all requests")


def _user_from_request(request: Request) -> dict[str, Any]:
    """Extract authenticated user identity from gateway-injected headers.

    In DEV_MODE without the gateway, fall back to a synthetic user so the
    dashboard is usable for local development.
    """
    if _DEV_MODE and not _sso_secret:
        return {"user_id": 1, "email": "dev@local"}
    uid_raw = request.headers.get("x-gateway-user-id", "")
    email = request.headers.get("x-gateway-user-email", "")
    try:
        uid = int(uid_raw)
    except ValueError:
        uid = 0
    if not uid or not email:
        # Should never happen if middleware passed, but guard anyway.
        raise HTTPException(status_code=401, detail="Missing gateway identity headers")
    return {"user_id": uid, "email": email}


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
    # script-src: 'unsafe-inline' removed — all JS lives in /static/app.js
    # (no inline <script> blocks, no inline on*= handlers). Matches the
    # gateway's hardened embed CSP shape.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )
    if _sso_secret:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


# ──────────────────────────────────────────────────────────────────────────────
# Database
# ──────────────────────────────────────────────────────────────────────────────

_db_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _init_db() -> None:
    schema = SCHEMA_SQL.read_text(encoding="utf-8")
    with _db_lock, _db() as conn:
        conn.executescript(schema)
        conn.commit()


_init_db()


@app.on_event("startup")
async def _on_startup() -> None:
    # Best-effort warm-up of the markets cache so the first request is fast.
    asyncio.create_task(markets.warmup())
    # Seed curated impact chains (idempotent: only inserts on empty table).
    try:
        _seed_impact_chains()
    except Exception as e:
        log.warning("impact_chains seed failed: %s", e)
    # Pre-warm news for the highest-traffic countries so the first drawer
    # open feels instant. The rest fetch lazily on demand.
    try:
        countries = _load_countries()["countries"]
        priority = [c for c in countries if c.get("tier") == "A"][:8]
        isos = [c["iso"] for c in priority]
        names = {c["iso"]: c.get("name", "") for c in priority}
        asyncio.create_task(news.warmup_top(isos, names))
    except Exception as e:
        log.warning("news warmup setup failed: %s", e)


# ──────────────────────────────────────────────────────────────────────────────
# Country data — loaded from YAML, overlaid with ETL cache files when present
# ──────────────────────────────────────────────────────────────────────────────

_COUNTRY_CACHE: dict[str, Any] = {"data": None, "loaded_at": 0.0}
_COUNTRY_CACHE_TTL = 60  # seconds; ETL writes cache files, server re-reads


def _load_etl_overlay(name: str) -> dict[str, Any]:
    """Load a single ETL overlay, returning {} if missing or malformed.

    Two-tier read so the service serves data on a fresh deploy without
    requiring any fetcher to have run yet:

      1. ``data/cache/{name}.json``     — hot path; written by ETL scripts.
      2. ``data/snapshot_{name}.yaml``  — committed last-known-good fallback.
    """
    json_path = CACHE_DIR / f"{name}.json"
    if json_path.exists():
        try:
            return json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("ETL overlay %s JSON unreadable: %s", name, e)

    yaml_path = DATA_DIR / f"snapshot_{name}.yaml"
    if yaml_path.exists():
        try:
            return yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            log.warning("ETL overlay %s snapshot unreadable: %s", name, e)
    return {}


def _load_countries() -> dict[str, Any]:
    """Load countries.yaml and overlay any ETL cache."""
    now = time.time()
    if _COUNTRY_CACHE["data"] and (now - _COUNTRY_CACHE["loaded_at"]) < _COUNTRY_CACHE_TTL:
        return _COUNTRY_CACHE["data"]

    with COUNTRIES_YAML.open("r", encoding="utf-8") as f:
        base = yaml.safe_load(f) or {}

    countries = base.get("countries", []) or []

    # Overlay ETL pulls. Each pull is keyed by ISO3.
    vdem = _load_etl_overlay("vdem").get("by_iso", {})
    wb = _load_etl_overlay("worldbank").get("by_iso", {})
    elections = _load_etl_overlay("elections_calendar").get("by_iso", {})
    pew = _load_etl_overlay("pew").get("by_iso", {})

    for c in countries:
        iso = c.get("iso")
        if not iso:
            continue
        if iso in vdem and isinstance(vdem[iso], dict):
            c.setdefault("democracy", {}).update(vdem[iso])
            c["democracy"]["_overlay_source"] = "v-dem"
        if iso in wb and isinstance(wb[iso], dict):
            for k, v in wb[iso].items():
                if v is not None:
                    c[k] = v
            c["_demographics_overlay_source"] = "world-bank"
        if iso in elections and isinstance(elections[iso], list) and elections[iso]:
            # Merge curated entries with ETL entries, dedupe by (date, type).
            merged = {(e.get("date"), e.get("type")): e for e in c.get("elections", [])}
            for e in elections[iso]:
                merged[(e.get("date"), e.get("type"))] = {**merged.get((e.get("date"), e.get("type")), {}), **e}
            c["elections"] = sorted(merged.values(), key=lambda e: e.get("date") or "")
        if iso in pew and isinstance(pew[iso], list) and pew[iso]:
            c["pew_findings"] = pew[iso]
            c["_pew_overlay_source"] = "pew-research"

    out = {
        "schema_version": base.get("schema_version", 1),
        "last_curated": base.get("last_curated"),
        "countries": countries,
    }
    _COUNTRY_CACHE["data"] = out
    _COUNTRY_CACHE["loaded_at"] = now
    return out


# ── Political context (leader / parties / regions) ─────────────────────────

_POLITICAL_CACHE: dict[str, Any] = {"data": None, "loaded_at": 0.0}
_POLITICAL_TTL = 60


def _load_political_context() -> dict[str, Any]:
    """Load political_context.yaml. Returns the full YAML dict."""
    now = time.time()
    if _POLITICAL_CACHE["data"] and (now - _POLITICAL_CACHE["loaded_at"]) < _POLITICAL_TTL:
        return _POLITICAL_CACHE["data"]
    if not POLITICAL_YAML.exists():
        out: dict[str, Any] = {"countries": {}}
    else:
        with POLITICAL_YAML.open("r", encoding="utf-8") as f:
            out = yaml.safe_load(f) or {"countries": {}}
    # Normalise: ensure parties are sorted by position (left → right).
    for iso, block in (out.get("countries") or {}).items():
        parties = block.get("parties") or []
        parties.sort(key=lambda p: (p.get("position") if p.get("position") is not None else 0))
        block["parties"] = parties
    _POLITICAL_CACHE["data"] = out
    _POLITICAL_CACHE["loaded_at"] = now
    return out


def _political_for_country(iso: str) -> dict[str, Any]:
    """Per-ISO political context: leader, parties, regions. Empty dict if absent."""
    pol = _load_political_context()
    block = (pol.get("countries") or {}).get(iso) or {}
    return {
        "leader": block.get("leader"),
        "parties": block.get("parties") or [],
        "regions": block.get("regions") or [],
    }


# ── Voter-influence overlay (economic / trust / identity / info / crisis) ─

_INFLUENCES_CACHE: dict[str, Any] = {"data": None, "extra": None, "loaded_at": 0.0}
_INFLUENCES_TTL = 60


def _load_influences() -> tuple[dict[str, Any], dict[str, Any]]:
    """Load both the base voter_influences.yaml and the extras file.

    Returns (base_dict, extras_dict). Extras add fields under
    `trust_extra`, `identity_extra`, and `security` per country.
    """
    now = time.time()
    if (
        _INFLUENCES_CACHE["data"] is not None
        and _INFLUENCES_CACHE["extra"] is not None
        and (now - _INFLUENCES_CACHE["loaded_at"]) < _INFLUENCES_TTL
    ):
        return _INFLUENCES_CACHE["data"], _INFLUENCES_CACHE["extra"]

    if INFLUENCES_YAML.exists():
        with INFLUENCES_YAML.open("r", encoding="utf-8") as f:
            base = yaml.safe_load(f) or {"countries": {}}
    else:
        base = {"countries": {}}

    if INFLUENCES_EXTRA_YAML.exists():
        with INFLUENCES_EXTRA_YAML.open("r", encoding="utf-8") as f:
            extra = yaml.safe_load(f) or {"countries": {}}
    else:
        extra = {"countries": {}}

    _INFLUENCES_CACHE["data"] = base
    _INFLUENCES_CACHE["extra"] = extra
    _INFLUENCES_CACHE["loaded_at"] = now
    return base, extra


def _influences_for_country(iso: str) -> dict[str, Any]:
    base, extra = _load_influences()
    block = (base.get("countries") or {}).get(iso) or {}
    extra_block = (extra.get("countries") or {}).get(iso) or {}

    # Merge trust_extra into trust, identity_extra into identity. Security
    # is its own block. This keeps the frontend's mental model simple:
    # `inf.trust` gets all trust fields whether base or extra.
    merged_trust = dict(block.get("trust") or {})
    merged_trust.update(extra_block.get("trust_extra") or {})

    merged_identity = dict(block.get("identity") or {})
    merged_identity.update(extra_block.get("identity_extra") or {})

    return {
        "economic_pressure": block.get("economic_pressure") or {},
        "trust": merged_trust,
        "identity": merged_identity,
        "information": block.get("information") or {},
        "crisis_memory": block.get("crisis_memory") or [],
        "demographic_shifts": block.get("demographic_shifts") or {},
        "security": extra_block.get("security") or {},
    }


# ── Polls (slice 2) ─────────────────────────────────────────────────────────

_POLLS_CACHE: dict[str, Any] = {"data": None, "loaded_at": 0.0}
_POLLS_TTL = 60


def _load_polls() -> dict[str, Any]:
    """Load polls.yaml. Returns the full YAML dict."""
    now = time.time()
    if _POLLS_CACHE["data"] and (now - _POLLS_CACHE["loaded_at"]) < _POLLS_TTL:
        return _POLLS_CACHE["data"]
    if not POLLS_YAML.exists():
        out = {"countries": {}}
    else:
        with POLLS_YAML.open("r", encoding="utf-8") as f:
            out = yaml.safe_load(f) or {"countries": {}}
    # Normalise: ensure each country has a polls list, sorted by date asc.
    for iso, block in (out.get("countries") or {}).items():
        polls = block.get("polls") or []
        polls.sort(key=lambda p: (p.get("date") or ""))
        block["polls"] = polls
    _POLLS_CACHE["data"] = out
    _POLLS_CACHE["loaded_at"] = now
    return out


def _polls_for_country(iso: str) -> dict[str, Any]:
    polls_data = _load_polls()
    block = (polls_data.get("countries") or {}).get(iso)
    if not block:
        return {"iso": iso, "polls": [], "election_label": None, "options": [], "series": []}

    polls = block.get("polls") or []
    election_label = block.get("election_label")

    # Build a stable option order by first-seen across polls.
    seen: list[str] = []
    seen_set: set[str] = set()
    for p in polls:
        for o in p.get("options") or []:
            label = o.get("label")
            if label and label not in seen_set:
                seen_set.add(label)
                seen.append(label)

    # Long-form series for plotting: each option gets a list of {date, pct}.
    series: list[dict] = []
    for label in seen:
        points = []
        for p in polls:
            opts = {o.get("label"): o.get("pct") for o in (p.get("options") or [])}
            if label in opts and opts[label] is not None:
                points.append({"date": p.get("date"), "pct": opts[label], "pollster": p.get("pollster")})
        if points:
            series.append({"label": label, "points": points})

    return {
        "iso": iso,
        "election_label": election_label,
        "options": seen,
        "polls": polls,
        "series": series,
    }


def _country_summary(c: dict) -> dict:
    """Cut down a country record to map-friendly summary."""
    return {
        "iso": c.get("iso"),
        "name": c.get("name"),
        "tier": c.get("tier"),
        "region": c.get("region"),
        "centroid": c.get("centroid"),
        "population_m": c.get("population_m"),
        "next_election": (c.get("elections") or [{}])[0].get("date") if c.get("elections") else None,
        "top_issue": (c.get("issue_salience") or [{}])[0].get("issue") if c.get("issue_salience") else None,
        "top_issue_pct": (c.get("issue_salience") or [{}])[0].get("pct") if c.get("issue_salience") else None,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Validation / sanitisation helpers
# ──────────────────────────────────────────────────────────────────────────────

_ISO_RE = re.compile(r"^[A-Z]{3}$")
_TARGET_TYPES = {"country", "poll", "election"}  # 'chain' / 'step' added later
_KIND_TYPES = {"comment", "reaction"}
_REACTION_RE = re.compile(r"^[a-z_]{1,32}$")  # emoji shortcode like 'thumbs_up'

MAX_BODY_LEN = 4000
MAX_REPLY_DEPTH = 6
COMMENT_RATE_LIMIT_PER_HOUR = 10
FLAG_RATE_LIMIT_PER_HOUR = 30
AUTO_HIDE_FLAG_THRESHOLD = 3


def _validate_target(target_type: str, target_id: str) -> None:
    if target_type not in _TARGET_TYPES:
        raise HTTPException(status_code=400, detail=f"target_type must be one of {sorted(_TARGET_TYPES)}")
    if not target_id or len(target_id) > 64:
        raise HTTPException(status_code=400, detail="target_id missing or too long")
    if target_type == "country":
        if not _ISO_RE.match(target_id):
            raise HTTPException(status_code=400, detail="country target_id must be ISO3")
        # Verify country actually exists
        countries = _load_countries()["countries"]
        if not any(c["iso"] == target_id for c in countries):
            raise HTTPException(status_code=404, detail="country not found")


def _check_rate_limit(conn: sqlite3.Connection, user_id: int, action: str, limit_per_hour: int) -> None:
    cutoff = int(time.time()) - 3600
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM rate_limit_log WHERE user_id=? AND action=? AND created_at>=?",
        (user_id, action, cutoff),
    ).fetchone()
    if row["c"] >= limit_per_hour:
        raise HTTPException(status_code=429, detail=f"rate limit: {limit_per_hour} {action}/hour")
    conn.execute(
        "INSERT INTO rate_limit_log (user_id, action, created_at) VALUES (?, ?, ?)",
        (user_id, action, int(time.time())),
    )
    # Opportunistic cleanup of old rows
    conn.execute("DELETE FROM rate_limit_log WHERE created_at < ?", (cutoff - 3600,))


def _audit(conn: sqlite3.Connection, actor: dict, action: str, target_type: str, target_id: str, meta: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_log (actor_id, actor_email, action, target_type, target_id, meta_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (actor["user_id"], actor["email"], action, target_type, target_id, json.dumps(meta or {}), int(time.time())),
    )


def _row_to_thought(r: sqlite3.Row, viewer_id: int | None = None) -> dict:
    out = {
        "id": r["id"],
        "user_id": r["user_id"],
        "user_email": r["user_email"],
        "target_type": r["target_type"],
        "target_id": r["target_id"],
        "kind": r["kind"],
        "body": r["body"],
        "parent_id": r["parent_id"],
        "created_at": r["created_at"],
        "edited_at": r["edited_at"],
        "hidden": r["hidden_at"] is not None,
        "upvotes": r["upvotes"],
        "downvotes": r["downvotes"],
    }
    # Don't leak hidden bodies to non-author viewers
    if r["hidden_at"] is not None and viewer_id != r["user_id"]:
        out["body"] = None
        out["hidden_reason"] = r["hidden_reason"]
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Routes — pages
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    if HTML_PATH.exists():
        return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Voters Dashboard</h1><p>UI not yet built.</p>")


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": "voters-dashboard", "ts": int(time.time())}


# ──────────────────────────────────────────────────────────────────────────────
# Routes — countries & elections
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/countries")
async def api_countries() -> dict:
    """List of all countries (summary form, for the world map)."""
    data = _load_countries()
    return {
        "schema_version": data["schema_version"],
        "last_curated": data["last_curated"],
        "countries": [_country_summary(c) for c in data["countries"]],
    }


@app.get("/api/country/{iso}")
async def api_country(iso: str, request: Request) -> dict:
    """Full country record."""
    iso = iso.upper()
    if not _ISO_RE.match(iso):
        raise HTTPException(status_code=400, detail="iso must be 3 letters")
    countries = _load_countries()["countries"]
    match = next((c for c in countries if c["iso"] == iso), None)
    if not match:
        raise HTTPException(status_code=404, detail="country not found")

    # Add cross-dashboard cross-links so the UI can render "see related markets"
    cross = await cross_dashboard.fetch_for_country(iso, match.get("name", ""))
    commodities = list(match.get("commodities_export") or []) + list(match.get("commodities_import") or [])
    cross["commodity_links"] = cross_dashboard.commodity_links(commodities)
    cross.pop("commodity_link_resolver", None)

    # Slice 2: prediction markets + polling summary.
    market_data = await markets.fetch_markets_for_country(iso, match.get("name", ""))
    polling = _polls_for_country(iso)
    polling_summary = {
        "has_polls": bool(polling["polls"]),
        "poll_count": len(polling["polls"]),
        "latest_date": polling["polls"][-1]["date"] if polling["polls"] else None,
        "election_label": polling["election_label"],
    }

    # Slice 5: political context (leader / parties / regions).
    political = _political_for_country(iso)
    # Slice 6: voter-influence dimensions (economic / trust / identity /
    # information / crisis memory / demographic shifts).
    influences = _influences_for_country(iso)

    return {
        **match,
        "_cross": cross,
        "_markets": market_data,
        "_polling_summary": polling_summary,
        "_political": political,
        "_influences": influences,
    }


@app.get("/api/country/{iso}/polling")
async def api_country_polling(iso: str) -> dict:
    """Time-series polling for charting."""
    iso = iso.upper()
    if not _ISO_RE.match(iso):
        raise HTTPException(status_code=400, detail="iso must be 3 letters")
    return _polls_for_country(iso)


@app.get("/api/country/{iso}/markets")
async def api_country_markets(iso: str) -> dict:
    """Live Polymarket + Kalshi markets matching this country."""
    iso = iso.upper()
    if not _ISO_RE.match(iso):
        raise HTTPException(status_code=400, detail="iso must be 3 letters")
    countries = _load_countries()["countries"]
    match = next((c for c in countries if c["iso"] == iso), None)
    if not match:
        raise HTTPException(status_code=404, detail="country not found")
    return await markets.fetch_markets_for_country(iso, match.get("name", ""))


@app.get("/api/country/{iso}/news")
async def api_country_news(iso: str) -> dict:
    """Live political news headlines from Google News RSS, cached 10 min."""
    iso = iso.upper()
    if not _ISO_RE.match(iso):
        raise HTTPException(status_code=400, detail="iso must be 3 letters")
    countries = _load_countries()["countries"]
    match = next((c for c in countries if c["iso"] == iso), None)
    if not match:
        raise HTTPException(status_code=404, detail="country not found")
    return await news.fetch_news_for_country(iso, match.get("name", ""))


@app.get("/api/elections/calendar")
async def api_elections_calendar(months: int = Query(default=24, ge=1, le=120)) -> dict:
    """Flat chronological list of upcoming elections across all countries."""
    countries = _load_countries()["countries"]
    horizon = int(time.time()) + months * 30 * 24 * 3600

    items = []
    for c in countries:
        for e in c.get("elections", []) or []:
            date_str = (e.get("date") or "").strip()
            if not date_str or date_str.lower().startswith("tbd"):
                items.append({
                    "iso": c["iso"],
                    "country": c["name"],
                    "tier": c.get("tier"),
                    "date": date_str or "TBD",
                    "type": e.get("type"),
                    "stakes": e.get("stakes"),
                    "_sort_key": "9999-99-99",  # sort to end
                })
                continue
            # Best-effort parse YYYY-MM-DD
            try:
                ts = time.mktime(time.strptime(date_str[:10], "%Y-%m-%d"))
            except Exception:
                continue
            if ts > horizon:
                continue
            items.append({
                "iso": c["iso"],
                "country": c["name"],
                "tier": c.get("tier"),
                "date": date_str,
                "type": e.get("type"),
                "stakes": e.get("stakes"),
                "_sort_key": date_str,
            })

    items.sort(key=lambda x: x["_sort_key"])
    for it in items:
        it.pop("_sort_key", None)
    return {"items": items, "horizon_months": months}


# ──────────────────────────────────────────────────────────────────────────────
# Routes — thoughts (comments + reactions)
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/thoughts")
async def api_thoughts_list(
    request: Request,
    target_type: str = Query(...),
    target_id: str = Query(...),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict:
    user = _user_from_request(request)
    _validate_target(target_type, target_id)
    with _db_lock, _db() as conn:
        rows = conn.execute(
            "SELECT * FROM thoughts WHERE target_type=? AND target_id=? "
            "ORDER BY created_at ASC LIMIT ?",
            (target_type, target_id, limit),
        ).fetchall()
    return {"items": [_row_to_thought(r, viewer_id=user["user_id"]) for r in rows]}


@app.post("/api/thoughts")
async def api_thoughts_create(request: Request) -> dict:
    user = _user_from_request(request)
    body_json = await request.json()

    target_type = (body_json.get("target_type") or "").strip()
    target_id = (body_json.get("target_id") or "").strip()
    kind = (body_json.get("kind") or "").strip()
    text = (body_json.get("body") or "").strip()
    parent_id = body_json.get("parent_id")

    _validate_target(target_type, target_id)
    if kind not in _KIND_TYPES:
        raise HTTPException(status_code=400, detail=f"kind must be one of {sorted(_KIND_TYPES)}")

    if kind == "comment":
        if not text:
            raise HTTPException(status_code=400, detail="comment body required")
        if len(text) > MAX_BODY_LEN:
            raise HTTPException(status_code=400, detail=f"comment exceeds {MAX_BODY_LEN} chars")
        rate_action = "comment"
        rate_limit = COMMENT_RATE_LIMIT_PER_HOUR
    else:  # reaction
        if not _REACTION_RE.match(text):
            raise HTTPException(status_code=400, detail="reaction body must be emoji shortcode (a-z_, ≤32)")
        rate_action = "comment"  # share the comment bucket; reactions are cheap
        rate_limit = COMMENT_RATE_LIMIT_PER_HOUR * 5

    with _db_lock, _db() as conn:
        # Validate parent if provided
        depth = 0
        if parent_id is not None:
            try:
                parent_id = int(parent_id)
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="parent_id must be int")
            cursor = parent_id
            while cursor and depth < MAX_REPLY_DEPTH + 1:
                row = conn.execute("SELECT parent_id, target_type, target_id FROM thoughts WHERE id=?", (cursor,)).fetchone()
                if not row:
                    raise HTTPException(status_code=400, detail="parent not found")
                if row["target_type"] != target_type or row["target_id"] != target_id:
                    raise HTTPException(status_code=400, detail="parent target mismatch")
                cursor = row["parent_id"]
                depth += 1
            if depth >= MAX_REPLY_DEPTH:
                raise HTTPException(status_code=400, detail=f"max reply depth {MAX_REPLY_DEPTH}")

        # For reactions, dedupe: one reaction-emoji per user per target
        if kind == "reaction":
            existing = conn.execute(
                "SELECT id FROM thoughts WHERE user_id=? AND target_type=? AND target_id=? AND kind='reaction' AND body=? AND hidden_at IS NULL",
                (user["user_id"], target_type, target_id, text),
            ).fetchone()
            if existing:
                # Toggle off
                conn.execute("UPDATE thoughts SET hidden_at=?, hidden_by=?, hidden_reason='toggle' WHERE id=?",
                             (int(time.time()), user["user_id"], existing["id"]))
                _audit(conn, user, "reaction_toggle_off", target_type, target_id, {"thought_id": existing["id"]})
                conn.commit()
                return {"removed": existing["id"]}

        _check_rate_limit(conn, user["user_id"], rate_action, rate_limit)

        now = int(time.time())
        cur = conn.execute(
            "INSERT INTO thoughts (user_id, user_email, target_type, target_id, kind, body, parent_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user["user_id"], user["email"], target_type, target_id, kind, text, parent_id, now),
        )
        new_id = cur.lastrowid
        _audit(conn, user, "create", target_type, target_id, {"thought_id": new_id, "kind": kind})
        conn.commit()
        row = conn.execute("SELECT * FROM thoughts WHERE id=?", (new_id,)).fetchone()
    return _row_to_thought(row, viewer_id=user["user_id"])


@app.post("/api/thoughts/{thought_id}/flag")
async def api_thoughts_flag(thought_id: int, request: Request) -> dict:
    user = _user_from_request(request)
    payload = await request.json() if request.headers.get("content-length") else {}
    reason = (payload.get("reason") or "").strip()[:200] if isinstance(payload, dict) else ""

    with _db_lock, _db() as conn:
        target = conn.execute("SELECT * FROM thoughts WHERE id=?", (thought_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="thought not found")
        if target["user_id"] == user["user_id"]:
            raise HTTPException(status_code=400, detail="cannot flag your own thought")

        _check_rate_limit(conn, user["user_id"], "flag", FLAG_RATE_LIMIT_PER_HOUR)

        try:
            conn.execute(
                "INSERT INTO thought_flags (thought_id, user_id, reason, created_at) VALUES (?, ?, ?, ?)",
                (thought_id, user["user_id"], reason, int(time.time())),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="already flagged by you")

        _audit(conn, user, "flag", target["target_type"], target["target_id"],
               {"thought_id": thought_id, "reason": reason})

        # Auto-hide if threshold reached and not already hidden
        flag_count = conn.execute(
            "SELECT COUNT(*) AS c FROM thought_flags WHERE thought_id=?", (thought_id,)
        ).fetchone()["c"]
        auto_hidden = False
        if flag_count >= AUTO_HIDE_FLAG_THRESHOLD and target["hidden_at"] is None:
            conn.execute(
                "UPDATE thoughts SET hidden_at=?, hidden_by=NULL, hidden_reason=? WHERE id=?",
                (int(time.time()), f"auto-hidden after {flag_count} flags", thought_id),
            )
            _audit(conn, user, "auto_hide", target["target_type"], target["target_id"],
                   {"thought_id": thought_id, "flag_count": flag_count})
            auto_hidden = True

        conn.commit()

    return {"ok": True, "flag_count": flag_count, "auto_hidden": auto_hidden}


@app.post("/api/thoughts/{thought_id}/vote")
async def api_thoughts_vote(thought_id: int, request: Request) -> dict:
    user = _user_from_request(request)
    payload = await request.json()
    try:
        vote = int(payload.get("vote", 0))
    except (TypeError, ValueError):
        vote = 0
    if vote not in (-1, 0, 1):
        raise HTTPException(status_code=400, detail="vote must be -1, 0, or 1")

    with _db_lock, _db() as conn:
        target = conn.execute("SELECT * FROM thoughts WHERE id=?", (thought_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="thought not found")

        prev = conn.execute(
            "SELECT vote FROM thought_votes WHERE thought_id=? AND user_id=?",
            (thought_id, user["user_id"]),
        ).fetchone()
        prev_vote = prev["vote"] if prev else 0

        if vote == 0:
            conn.execute("DELETE FROM thought_votes WHERE thought_id=? AND user_id=?",
                         (thought_id, user["user_id"]))
        else:
            conn.execute(
                "INSERT INTO thought_votes (thought_id, user_id, vote, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(thought_id, user_id) DO UPDATE SET vote=excluded.vote, created_at=excluded.created_at",
                (thought_id, user["user_id"], vote, int(time.time())),
            )

        # Recompute aggregates
        agg = conn.execute(
            "SELECT "
            "  COALESCE(SUM(CASE WHEN vote=1 THEN 1 ELSE 0 END), 0) AS up, "
            "  COALESCE(SUM(CASE WHEN vote=-1 THEN 1 ELSE 0 END), 0) AS dn "
            "FROM thought_votes WHERE thought_id=?",
            (thought_id,),
        ).fetchone()
        conn.execute("UPDATE thoughts SET upvotes=?, downvotes=? WHERE id=?",
                     (agg["up"], agg["dn"], thought_id))

        _audit(conn, user, "vote", target["target_type"], target["target_id"],
               {"thought_id": thought_id, "from": prev_vote, "to": vote})
        conn.commit()

    return {"ok": True, "upvotes": agg["up"], "downvotes": agg["dn"], "your_vote": vote}


@app.delete("/api/thoughts/{thought_id}")
async def api_thoughts_delete(thought_id: int, request: Request) -> dict:
    """Soft-delete: only the author can hide their own thought (slice 1).
    Reviewer hide/unhide arrives in slice 3 with the role system."""
    user = _user_from_request(request)
    with _db_lock, _db() as conn:
        row = conn.execute("SELECT * FROM thoughts WHERE id=?", (thought_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="thought not found")
        if row["user_id"] != user["user_id"]:
            raise HTTPException(status_code=403, detail="only the author can delete (slice 1)")
        if row["hidden_at"] is not None:
            return {"ok": True, "already_hidden": True}
        conn.execute(
            "UPDATE thoughts SET hidden_at=?, hidden_by=?, hidden_reason='author-deleted' WHERE id=?",
            (int(time.time()), user["user_id"], thought_id),
        )
        _audit(conn, user, "hide", row["target_type"], row["target_id"], {"thought_id": thought_id, "by": "author"})
        conn.commit()
    return {"ok": True}


# ──────────────────────────────────────────────────────────────────────────────
# Routes — impact chains (slice 3) + counter-chains (slice 4)
# ──────────────────────────────────────────────────────────────────────────────
#
# Lifecycle:
#   draft       — author still editing; visible only to author/reviewer/admin
#   under_review — submitted for review; visible to reviewers + author
#   approved    — visible to all subscribers; appears in country drawer
#   rejected    — soft-rejected; visible to author with notes; not in drawer
#
# Chain creation rate-limit: CHAIN_CREATE_RATE_LIMIT_PER_HOUR drafts/user/hr
# to deter spam. Only authors and reviewers can mutate; only reviewers can
# transition status forward into approved/rejected.

CHAIN_KIND_ENUM = {"concern", "actor", "policy", "market", "evidence"}
CHAIN_STATUS_ENUM = {"draft", "under_review", "approved", "rejected"}
CHAIN_COUNTER_KIND_ENUM = {"refute", "fork", "extend"}
CHAIN_CREATE_RATE_LIMIT_PER_HOUR = 5
CHAIN_MAX_STEPS = 8
CHAIN_MIN_STEPS = 2


def _seed_impact_chains() -> None:
    """One-shot import of curated chains from impact_chains.yaml on empty DB."""
    if not CHAINS_YAML.exists():
        return
    with _db_lock, _db() as conn:
        existing = conn.execute("SELECT COUNT(*) AS c FROM impact_chains WHERE source_kind='seed'").fetchone()
        if existing["c"] > 0:
            return
        with CHAINS_YAML.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        chains = data.get("chains") or []
        seed_actor = (sorted(_ADMIN_EMAILS) or sorted(_REVIEWER_EMAILS) or ["seed@curator"])[0]
        now = int(time.time())
        seeded = 0
        for ch in chains:
            iso = (ch.get("iso") or "").upper()
            title = (ch.get("title") or "").strip()
            if not iso or not title:
                continue
            cur = conn.execute(
                "INSERT INTO impact_chains "
                "(iso, title, summary, author_id, author_email, status, source_kind, "
                " created_at, submitted_at, decided_at, review_notes) "
                "VALUES (?, ?, ?, 0, ?, 'approved', 'seed', ?, ?, ?, 'curated seed')",
                (iso, title, ch.get("summary"), seed_actor, now, now, now),
            )
            chain_id = cur.lastrowid
            for idx, step in enumerate(ch.get("steps") or []):
                kind = (step.get("kind") or "").lower()
                if kind not in CHAIN_KIND_ENUM:
                    continue
                conn.execute(
                    "INSERT INTO impact_chain_steps "
                    "(chain_id, step_idx, kind, text, detail, ref_url, ref_provider, ref_id, confidence, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chain_id, idx, kind,
                        step.get("text") or "",
                        step.get("detail"),
                        step.get("ref_url"),
                        step.get("ref_provider"),
                        step.get("ref_id"),
                        step.get("confidence"),
                        now,
                    ),
                )
            seeded += 1
        conn.commit()
        log.info("impact_chains seeded %d curated chains", seeded)


def _validate_chain_steps(steps_payload: list[dict]) -> list[dict]:
    """Validate a chain's steps payload; raise HTTPException on issues."""
    if not isinstance(steps_payload, list):
        raise HTTPException(status_code=400, detail="steps must be a list")
    if not (CHAIN_MIN_STEPS <= len(steps_payload) <= CHAIN_MAX_STEPS):
        raise HTTPException(
            status_code=400,
            detail=f"chain must have between {CHAIN_MIN_STEPS} and {CHAIN_MAX_STEPS} steps",
        )
    cleaned: list[dict] = []
    for i, step in enumerate(steps_payload):
        if not isinstance(step, dict):
            raise HTTPException(status_code=400, detail=f"step {i} must be an object")
        kind = (step.get("kind") or "").lower()
        if kind not in CHAIN_KIND_ENUM:
            raise HTTPException(
                status_code=400,
                detail=f"step {i} kind must be one of {sorted(CHAIN_KIND_ENUM)}",
            )
        text = (step.get("text") or "").strip()
        if not text or len(text) > 240:
            raise HTTPException(status_code=400, detail=f"step {i} text required (≤240 chars)")
        detail = (step.get("detail") or "").strip() or None
        if detail and len(detail) > 1000:
            raise HTTPException(status_code=400, detail=f"step {i} detail too long (≤1000 chars)")
        ref_url = (step.get("ref_url") or "").strip() or None
        if ref_url and not ref_url.startswith(("https://", "http://")):
            raise HTTPException(status_code=400, detail=f"step {i} ref_url must be http(s)")
        confidence = step.get("confidence")
        if confidence is not None:
            try:
                confidence = int(confidence)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"step {i} confidence must be int 1..5")
            if not (1 <= confidence <= 5):
                raise HTTPException(status_code=400, detail=f"step {i} confidence must be 1..5")
        cleaned.append({
            "kind": kind,
            "text": text,
            "detail": detail,
            "ref_url": ref_url,
            "ref_provider": (step.get("ref_provider") or "").strip()[:32] or None,
            "ref_id": (step.get("ref_id") or "").strip()[:128] or None,
            "confidence": confidence,
        })
    return cleaned


def _chain_row_to_dict(r: sqlite3.Row, viewer: dict | None = None) -> dict:
    role = _user_role(viewer) if viewer else "anon"
    is_author = bool(viewer and viewer.get("user_id") == r["author_id"])
    return {
        "id": r["id"],
        "iso": r["iso"],
        "title": r["title"],
        "summary": r["summary"],
        "author_email": r["author_email"],
        "status": r["status"],
        "source_kind": r["source_kind"],
        "parent_chain_id": r["parent_chain_id"],
        "counter_kind": r["counter_kind"],
        "created_at": r["created_at"],
        "submitted_at": r["submitted_at"],
        "decided_at": r["decided_at"],
        "review_notes": r["review_notes"] if (role in ("reviewer", "admin") or is_author) else None,
        "upvotes": r["upvotes"],
        "downvotes": r["downvotes"],
        "is_author": is_author,
        "viewer_role": role,
    }


def _step_row_to_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "step_idx": r["step_idx"],
        "kind": r["kind"],
        "text": r["text"],
        "detail": r["detail"],
        "ref_url": r["ref_url"],
        "ref_provider": r["ref_provider"],
        "ref_id": r["ref_id"],
        "confidence": r["confidence"],
    }


def _load_chain_with_steps(conn: sqlite3.Connection, chain_id: int, viewer: dict | None = None) -> dict | None:
    row = conn.execute("SELECT * FROM impact_chains WHERE id=?", (chain_id,)).fetchone()
    if not row:
        return None
    steps = conn.execute(
        "SELECT * FROM impact_chain_steps WHERE chain_id=? ORDER BY step_idx",
        (chain_id,),
    ).fetchall()
    chain = _chain_row_to_dict(row, viewer)
    chain["steps"] = [_step_row_to_dict(s) for s in steps]
    return chain


@app.get("/api/country/{iso}/chains")
async def api_country_chains(iso: str, request: Request) -> dict:
    """List approved chains for a country, plus the viewer's own drafts."""
    iso = iso.upper()
    if not _ISO_RE.match(iso):
        raise HTTPException(status_code=400, detail="iso must be 3 letters")
    user = _user_from_request(request)
    role = _user_role(user)
    with _db_lock, _db() as conn:
        if role in ("reviewer", "admin"):
            # Reviewers see everything for the country
            rows = conn.execute(
                "SELECT * FROM impact_chains WHERE iso=? "
                "ORDER BY (status='approved') DESC, created_at DESC",
                (iso,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM impact_chains WHERE iso=? AND "
                "(status='approved' OR author_id=?) "
                "ORDER BY (status='approved') DESC, created_at DESC",
                (iso, user["user_id"]),
            ).fetchall()
        chains = []
        for r in rows:
            full = _load_chain_with_steps(conn, r["id"], user)
            if full:
                chains.append(full)
    return {"iso": iso, "chains": chains}


@app.get("/api/chains/{chain_id}")
async def api_chain_get(chain_id: int, request: Request) -> dict:
    user = _user_from_request(request)
    role = _user_role(user)
    with _db_lock, _db() as conn:
        chain = _load_chain_with_steps(conn, chain_id, user)
        if not chain:
            raise HTTPException(status_code=404, detail="chain not found")
        # Visibility: approved → all; otherwise author or reviewer/admin
        if chain["status"] != "approved":
            if not (chain["is_author"] or role in ("reviewer", "admin")):
                raise HTTPException(status_code=403, detail="not visible")
        # Counter-chains attached to this one
        counters = conn.execute(
            "SELECT * FROM impact_chains WHERE parent_chain_id=? "
            "AND (status='approved' OR author_id=?) ORDER BY created_at DESC",
            (chain_id, user["user_id"]),
        ).fetchall()
        chain["counter_chains"] = [_chain_row_to_dict(c, user) for c in counters]
        # Reviews trail
        reviews = conn.execute(
            "SELECT * FROM impact_chain_reviews WHERE chain_id=? ORDER BY created_at",
            (chain_id,),
        ).fetchall()
        chain["reviews"] = [
            {
                "reviewer_email": r["reviewer_email"],
                "decision": r["decision"],
                "notes": r["notes"],
                "created_at": r["created_at"],
            }
            for r in reviews
        ] if role in ("reviewer", "admin") or chain["is_author"] else []
        # Viewer's vote
        v = conn.execute(
            "SELECT vote FROM impact_chain_votes WHERE chain_id=? AND user_id=?",
            (chain_id, user["user_id"]),
        ).fetchone()
        chain["my_vote"] = v["vote"] if v else 0
    return chain


@app.post("/api/chains")
async def api_chain_create(request: Request) -> dict:
    """Create a new draft chain. Slice 4: pass parent_chain_id + counter_kind."""
    user = _user_from_request(request)
    payload = await request.json()
    iso = (payload.get("iso") or "").upper()
    if not _ISO_RE.match(iso):
        raise HTTPException(status_code=400, detail="iso must be 3 letters")
    title = (payload.get("title") or "").strip()
    if not title or len(title) > 200:
        raise HTTPException(status_code=400, detail="title required (≤200 chars)")
    summary = (payload.get("summary") or "").strip() or None
    if summary and len(summary) > 2000:
        raise HTTPException(status_code=400, detail="summary too long (≤2000 chars)")
    steps = _validate_chain_steps(payload.get("steps") or [])

    parent_id = payload.get("parent_chain_id")
    counter_kind = (payload.get("counter_kind") or "").lower() or None
    if parent_id is not None:
        try:
            parent_id = int(parent_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="parent_chain_id must be int")
        if counter_kind not in CHAIN_COUNTER_KIND_ENUM:
            raise HTTPException(
                status_code=400,
                detail=f"counter_kind required for counter-chain ({sorted(CHAIN_COUNTER_KIND_ENUM)})",
            )

    countries = _load_countries()["countries"]
    if not any(c["iso"] == iso for c in countries):
        raise HTTPException(status_code=404, detail="country not found")

    with _db_lock, _db() as conn:
        if parent_id is not None:
            parent = conn.execute("SELECT iso, status FROM impact_chains WHERE id=?", (parent_id,)).fetchone()
            if not parent:
                raise HTTPException(status_code=404, detail="parent chain not found")
            if parent["iso"] != iso:
                raise HTTPException(status_code=400, detail="counter-chain must share parent's country")
            if parent["status"] != "approved":
                raise HTTPException(status_code=400, detail="can only counter approved chains")

        _check_rate_limit(conn, user["user_id"], "chain_create", CHAIN_CREATE_RATE_LIMIT_PER_HOUR)

        now = int(time.time())
        cur = conn.execute(
            "INSERT INTO impact_chains "
            "(iso, title, summary, author_id, author_email, status, source_kind, "
            " parent_chain_id, counter_kind, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'draft', 'user', ?, ?, ?)",
            (iso, title, summary, user["user_id"], user["email"], parent_id, counter_kind, now),
        )
        chain_id = cur.lastrowid
        for idx, s in enumerate(steps):
            conn.execute(
                "INSERT INTO impact_chain_steps "
                "(chain_id, step_idx, kind, text, detail, ref_url, ref_provider, ref_id, confidence, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (chain_id, idx, s["kind"], s["text"], s["detail"], s["ref_url"],
                 s["ref_provider"], s["ref_id"], s["confidence"], now),
            )
        _audit(conn, user, "chain_create", "chain", str(chain_id),
               {"iso": iso, "step_count": len(steps), "parent_chain_id": parent_id})
        conn.commit()
        return _load_chain_with_steps(conn, chain_id, user)


@app.post("/api/chains/{chain_id}/submit")
async def api_chain_submit(chain_id: int, request: Request) -> dict:
    """Move a draft chain to under_review."""
    user = _user_from_request(request)
    with _db_lock, _db() as conn:
        row = conn.execute("SELECT * FROM impact_chains WHERE id=?", (chain_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="chain not found")
        if row["author_id"] != user["user_id"]:
            raise HTTPException(status_code=403, detail="only the author can submit")
        if row["status"] != "draft":
            raise HTTPException(status_code=400, detail=f"chain is {row['status']}, not draft")
        # Sanity: ensure the chain has the minimum step count.
        n = conn.execute("SELECT COUNT(*) AS c FROM impact_chain_steps WHERE chain_id=?", (chain_id,)).fetchone()
        if n["c"] < CHAIN_MIN_STEPS:
            raise HTTPException(status_code=400, detail=f"chain must have ≥ {CHAIN_MIN_STEPS} steps before submit")
        now = int(time.time())
        conn.execute(
            "UPDATE impact_chains SET status='under_review', submitted_at=? WHERE id=?",
            (now, chain_id),
        )
        _audit(conn, user, "chain_submit", "chain", str(chain_id), {})
        conn.commit()
        return _load_chain_with_steps(conn, chain_id, user)


@app.post("/api/chains/{chain_id}/review")
async def api_chain_review(chain_id: int, request: Request) -> dict:
    """Reviewer decision: approve | reject | request_changes | comment."""
    user = _user_from_request(request)
    _require_reviewer(user)
    payload = await request.json()
    decision = (payload.get("decision") or "").strip().lower()
    if decision not in {"approve", "reject", "request_changes", "comment"}:
        raise HTTPException(status_code=400, detail="decision must be approve|reject|request_changes|comment")
    notes = (payload.get("notes") or "").strip() or None
    if notes and len(notes) > 2000:
        raise HTTPException(status_code=400, detail="notes too long (≤2000 chars)")

    with _db_lock, _db() as conn:
        row = conn.execute("SELECT * FROM impact_chains WHERE id=?", (chain_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="chain not found")
        if row["status"] not in ("under_review", "approved", "rejected"):
            raise HTTPException(status_code=400, detail=f"chain is {row['status']}; cannot review")
        now = int(time.time())
        conn.execute(
            "INSERT INTO impact_chain_reviews "
            "(chain_id, reviewer_id, reviewer_email, decision, notes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chain_id, user["user_id"], user["email"], decision, notes, now),
        )
        if decision == "approve":
            conn.execute(
                "UPDATE impact_chains SET status='approved', decided_at=?, decided_by=?, review_notes=? WHERE id=?",
                (now, user["user_id"], notes, chain_id),
            )
        elif decision == "reject":
            conn.execute(
                "UPDATE impact_chains SET status='rejected', decided_at=?, decided_by=?, review_notes=? WHERE id=?",
                (now, user["user_id"], notes, chain_id),
            )
        elif decision == "request_changes":
            conn.execute(
                "UPDATE impact_chains SET status='draft', review_notes=? WHERE id=?",
                (notes, chain_id),
            )
        # 'comment' is a no-op on chain status; just appends a review row.
        _audit(conn, user, f"chain_{decision}", "chain", str(chain_id), {})
        conn.commit()
        return _load_chain_with_steps(conn, chain_id, user)


@app.post("/api/chains/{chain_id}/vote")
async def api_chain_vote(chain_id: int, request: Request) -> dict:
    user = _user_from_request(request)
    payload = await request.json()
    try:
        v = int(payload.get("vote", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="vote must be -1, 0, or 1")
    if v not in (-1, 0, 1):
        raise HTTPException(status_code=400, detail="vote must be -1, 0, or 1")

    with _db_lock, _db() as conn:
        row = conn.execute("SELECT id, status, author_id FROM impact_chains WHERE id=?", (chain_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="chain not found")
        if row["status"] != "approved":
            raise HTTPException(status_code=400, detail="can only vote on approved chains")
        if row["author_id"] == user["user_id"]:
            raise HTTPException(status_code=400, detail="cannot vote on your own chain")
        existing = conn.execute(
            "SELECT vote FROM impact_chain_votes WHERE chain_id=? AND user_id=?",
            (chain_id, user["user_id"]),
        ).fetchone()
        prev = existing["vote"] if existing else 0
        if v == 0:
            conn.execute("DELETE FROM impact_chain_votes WHERE chain_id=? AND user_id=?",
                         (chain_id, user["user_id"]))
        else:
            conn.execute(
                "INSERT INTO impact_chain_votes (chain_id, user_id, vote, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(chain_id, user_id) DO UPDATE SET vote=excluded.vote, created_at=excluded.created_at",
                (chain_id, user["user_id"], v, int(time.time())),
            )
        # Recompute totals
        agg = conn.execute(
            "SELECT "
            "  SUM(CASE WHEN vote=1 THEN 1 ELSE 0 END) AS up, "
            "  SUM(CASE WHEN vote=-1 THEN 1 ELSE 0 END) AS dn "
            "FROM impact_chain_votes WHERE chain_id=?",
            (chain_id,),
        ).fetchone()
        conn.execute(
            "UPDATE impact_chains SET upvotes=?, downvotes=? WHERE id=?",
            (agg["up"] or 0, agg["dn"] or 0, chain_id),
        )
        _audit(conn, user, "chain_vote", "chain", str(chain_id), {"prev": prev, "new": v})
        conn.commit()
        return {"chain_id": chain_id, "your_vote": v, "upvotes": agg["up"] or 0, "downvotes": agg["dn"] or 0}


@app.get("/api/reviewer/queue")
async def api_reviewer_queue(request: Request, status: str = Query("under_review")) -> dict:
    """Reviewer-only: list chains awaiting decision (default under_review)."""
    user = _user_from_request(request)
    _require_reviewer(user)
    if status not in CHAIN_STATUS_ENUM:
        raise HTTPException(status_code=400, detail="invalid status")
    with _db_lock, _db() as conn:
        rows = conn.execute(
            "SELECT * FROM impact_chains WHERE status=? ORDER BY submitted_at ASC, created_at ASC",
            (status,),
        ).fetchall()
        items = []
        for r in rows:
            chain = _load_chain_with_steps(conn, r["id"], user)
            if chain:
                items.append(chain)
    return {"role": _user_role(user), "status": status, "count": len(items), "items": items}


@app.get("/api/me")
async def api_me(request: Request) -> dict:
    """Identity + role for the frontend (so it can show the reviewer panel)."""
    user = _user_from_request(request)
    return {"user_id": user["user_id"], "email": user["email"], "role": _user_role(user)}


# ──────────────────────────────────────────────────────────────────────────────
# Routes — sharing for sibling dashboards (used by world-state, midterm, etc.)
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/share/snapshot")
async def api_share_snapshot() -> dict:
    """Lightweight summary other dashboards can pull. Localhost-only by design."""
    data = _load_countries()
    upcoming = []
    now = int(time.time())
    horizon = now + 6 * 30 * 24 * 3600  # 6 months
    for c in data["countries"]:
        for e in c.get("elections", []) or []:
            date_str = (e.get("date") or "")[:10]
            try:
                ts = time.mktime(time.strptime(date_str, "%Y-%m-%d"))
            except Exception:
                continue
            if now <= ts <= horizon:
                upcoming.append({
                    "iso": c["iso"], "country": c["name"],
                    "date": date_str, "type": e.get("type"),
                })
    upcoming.sort(key=lambda x: x["date"])
    return {
        "country_count": len(data["countries"]),
        "upcoming_elections_6m": upcoming[:20],
        "last_curated": data["last_curated"],
    }


# ──────────────────────────────────────────────────────────────────────────────
# Catch-all 404 (never serve gateway-only paths from here)
# ──────────────────────────────────────────────────────────────────────────────

@app.exception_handler(404)
async def not_found(request: Request, exc):  # type: ignore[no-untyped-def]
    return JSONResponse({"error": "not found", "path": request.url.path}, status_code=404)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("BIND_HOST", "127.0.0.1"), port=7051)

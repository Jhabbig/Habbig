#!/usr/bin/env python3
"""
Whale Watch — FastAPI backend.

Tracks SEC 13F-HR / 13D / Form 4 filings from a curated roster of
institutional "whales" (BlackRock, Berkshire, Pershing Square, Icahn,
Citadel, ...) and surfaces a unified feed, per-filer position views,
and "consensus" detection where N+ whales hold the same security.

This is the MVP scaffold:

  * Whale roster is seeded from data/whales.yaml on boot.
  * Filings tables exist but are empty — real ingestion happens via
    scripts/seed_13f.py (a placeholder; see comments there).
  * /api/recent-filings is stubbed with a small sample so the front
    end has something to render before the seeder runs against EDGAR.

Authentication: gateway-injected SSO headers. Mirrors the pattern
in voters-dashboard/. In DEV_MODE without a secret, a synthetic
user is returned so the dashboard is usable locally.

Port: 8053 (matches gateway/config.json → whale.target).
"""

from __future__ import annotations

# ── Observability — init Sentry FIRST, before FastAPI touches anything ───────
import observability as _observability
_observability.init_sentry(platform="whale")

import asyncio
import hmac
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Make ``scripts/`` importable so we can call the EDGAR seeder from startup.
_SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# ──────────────────────────────────────────────────────────────────────────────
# App init
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Whale Watch")
log = logging.getLogger("whale")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


# ── BetterStack / Logtail ─────────────────────────────────────────────────────
# Ships structured logs to the central BetterStack source for the "whale"
# subproduct. Falls back to the apex LOGTAIL_TOKEN if the per-service variable
# is unset. If neither is set we silently skip — stdout/stderr handlers stay
# attached so logs are never lost.
class _ServiceTagFilter(logging.Filter):
    """Stamps every record with service=<name> so BetterStack can route/group."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service = service_name

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        if not hasattr(record, "service"):
            record.service = self._service
        return True


_logtail_token = os.getenv("LOGTAIL_TOKEN_WHALE", os.getenv("LOGTAIL_TOKEN", "")).strip()
# Always tag local records with the service name so downstream aggregators
# (docker logs -> vector -> wherever) can group correctly even without Logtail.
logging.getLogger().addFilter(_ServiceTagFilter("whale"))
if _logtail_token:
    try:
        from logtail import LogtailHandler  # type: ignore

        _handler = LogtailHandler(source_token=_logtail_token)
        _handler.setLevel(logging.INFO)
        _handler.addFilter(_ServiceTagFilter("whale"))
        logging.getLogger().addHandler(_handler)
        log.info("Logtail handler attached", extra={"service": "whale"})
    except ImportError:
        log.warning("logtail-python not installed; skipping BetterStack handler",
                    extra={"service": "whale"})
    except Exception as _exc:  # pragma: no cover — defensive: never crash on log init
        log.warning("Logtail init failed: %s", _exc, extra={"service": "whale"})


ROOT = Path(__file__).parent
HTML_PATH = ROOT / "static" / "index.html"
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
SCHEMA_SQL = ROOT / "schema.sql"
WHALES_YAML = DATA_DIR / "whales.yaml"
DB_PATH = Path(os.environ.get("WHALE_DB_PATH", str(ROOT / "whale.sqlite")))

CACHE_DIR.mkdir(parents=True, exist_ok=True)

# CORS — narve.ai + subdomains (whale.narve.ai is what the gateway proxies).
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://([a-z0-9-]+\.)?narve\.ai(:\d+)?$|^http://localhost(:\d+)?$",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ──────────────────────────────────────────────────────────────────────────────
# Auth (gateway SSO) — mirrors voters-dashboard pattern.
#
# Without HMAC verification of the gateway's shared secret, anything that can
# reach this port can forge ``X-Gateway-User-Id`` / ``X-Gateway-User-Email``
# and impersonate any user. The middleware below rejects every request whose
# ``X-Gateway-Secret`` header doesn't match the server-side secret (constant
# time compare), so identity headers can only originate from the gateway.
# Combined with binding 127.0.0.1, the dashboard is only reachable through
# the gateway proxy on the same host.
# ──────────────────────────────────────────────────────────────────────────────

_sso_secret = os.environ.get("GATEWAY_SSO_SECRET", "")
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"

if not _sso_secret and not _DEV_MODE:
    log.warning("GATEWAY_SSO_SECRET unset and DEV_MODE off — every gateway-fronted request will 401.")


# Paths that bypass auth: health probes for systemd/Docker + public static
# assets + PWA install probes. Everything else requires a verified gateway secret.
_AUTH_BYPASS_EXACT = {"/health", "/healthz", "/favicon.ico", "/manifest.webmanifest"}


@app.middleware("http")
async def gateway_auth(request: Request, call_next):
    path = request.url.path
    # Let CORS preflights through so CORSMiddleware can handle them.
    if request.method == "OPTIONS":
        return await call_next(request)
    if path in _AUTH_BYPASS_EXACT or path.startswith("/static/"):
        return await call_next(request)
    if _DEV_MODE and not _sso_secret:
        # Local development without a gateway — synthetic user is fine.
        return await call_next(request)
    if not _sso_secret:
        return JSONResponse({"error": "service misconfigured"}, status_code=503)
    client_secret = request.headers.get("x-gateway-secret", "")
    if not hmac.compare_digest(client_secret, _sso_secret):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


def _user_from_request(request: Request) -> Optional[dict[str, Any]]:
    """Return the authenticated user dict or None. Read endpoints tolerate
    anonymous; write endpoints call ``_require_user``.

    The ``gateway_auth`` middleware has already verified ``X-Gateway-Secret``
    by the time this runs, so the identity headers are trustworthy.
    """
    if _DEV_MODE and not _sso_secret:
        return {"user_id": 1, "email": "dev@local"}
    uid_raw = request.headers.get("x-gateway-user-id", "")
    email = request.headers.get("x-gateway-user-email", "")
    try:
        uid = int(uid_raw)
    except ValueError:
        return None
    if not uid or not email:
        return None
    return {"user_id": uid, "email": email}


def _require_user(request: Request) -> dict[str, Any]:
    u = _user_from_request(request)
    if not u:
        raise HTTPException(status_code=401, detail="auth required")
    return u


# ──────────────────────────────────────────────────────────────────────────────
# Database — single connection guarded by a lock. SQLite handles concurrent
# reads fine via WAL; writes serialise behind the lock.
# ──────────────────────────────────────────────────────────────────────────────

_db_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_conn: sqlite3.Connection = _connect()


def _init_schema() -> None:
    if not SCHEMA_SQL.exists():
        log.error("schema.sql missing at %s", SCHEMA_SQL)
        return
    with _db_lock:
        _conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
        _conn.commit()
    log.info("schema applied to %s", DB_PATH)


def _startup_vacuum() -> None:
    """Compact whale.sqlite on boot.

    The gateway has its own daily VACUUM job for ``auth.db`` (see
    gateway/jobs/db_maintenance.py), but the subproduct DBs were never
    maintained. Running VACUUM + ANALYZE + WAL-truncate at boot keeps
    the file compact between deploys (daily redeploys) and refreshes
    the planner stats. Best-effort: failures are logged and swallowed
    so a wedged DB does not block startup.
    """
    import os
    try:
        size_before = os.path.getsize(DB_PATH) if DB_PATH.exists() else None
    except OSError:
        size_before = None
    try:
        with _db_lock:
            _conn.execute("VACUUM")
            _conn.execute("ANALYZE")
            _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            _conn.commit()
        try:
            size_after = os.path.getsize(DB_PATH) if DB_PATH.exists() else None
        except OSError:
            size_after = None
        log.info(
            "whale startup VACUUM: size_before=%s size_after=%s",
            size_before, size_after,
        )
    except sqlite3.Error as e:
        log.warning("whale startup VACUUM failed (continuing): %s", e)


def _seed_whales() -> None:
    """Upsert the YAML roster into ``whales`` on boot."""
    if not WHALES_YAML.exists():
        log.warning("whales.yaml missing at %s — no whales seeded", WHALES_YAML)
        return
    try:
        doc = yaml.safe_load(WHALES_YAML.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        log.error("whales.yaml parse failed: %s", e)
        return
    rows = doc.get("whales") or []
    now = int(time.time())
    inserted = 0
    seen_ciks: set[str] = set()
    with _db_lock:
        for w in rows:
            cik = (w.get("cik") or "").strip()
            name = (w.get("name") or "").strip()
            if not name:
                continue
            # If CIK is missing, non-numeric, or duplicates an earlier entry
            # (the seed roster carries placeholder CIKs that we haven't
            # verified against EDGAR yet), synthesise a stable non-EDGAR key
            # from the name. Real CIKs are 10-digit numeric strings; the
            # 'X' prefix lets the EDGAR seeder skip these rows later.
            if not cik or not cik.isdigit() or cik in seen_ciks:
                cik = "X" + "".join(c for c in name.upper() if c.isalnum())[:18].ljust(9, "0")
            seen_ciks.add(cik)
            _conn.execute(
                """
                INSERT INTO whales (cik, name, short_name, kind, aum_usd_b,
                                    twitter, website, notes, is_active,
                                    created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(cik) DO UPDATE SET
                    name = excluded.name,
                    short_name = excluded.short_name,
                    kind = excluded.kind,
                    aum_usd_b = excluded.aum_usd_b,
                    twitter = excluded.twitter,
                    website = excluded.website,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                (
                    cik,
                    name,
                    w.get("short_name"),
                    w.get("kind") or "fund",
                    w.get("aum_usd_b"),
                    w.get("twitter"),
                    w.get("website"),
                    w.get("notes"),
                    now,
                    now,
                ),
            )
            inserted += 1
        _conn.commit()
    log.info("seeded %d whales", inserted)


# ──────────────────────────────────────────────────────────────────────────────
# EDGAR ingestion — backgrounded so startup isn't blocked on network I/O.
#
# Two phases:
#   1. If the filings tables are empty (fresh install), run one full sweep
#      immediately. This populates the Live Feed without waiting a day.
#   2. Every ``EDGAR_REFRESH_SECONDS`` (default 24h), refresh the recent
#      filings index for every active whale. INSERT OR IGNORE keeps the
#      sweep idempotent — re-running costs at most a few CIK lookups.
#
# We import ``seed_13f`` lazily inside the task so a missing httpx in
# some test/CI environment can't break server import.
# ──────────────────────────────────────────────────────────────────────────────

# 24h. Override with ``EDGAR_REFRESH_SECONDS`` for local debugging.
EDGAR_REFRESH_SECONDS = int(os.environ.get("EDGAR_REFRESH_SECONDS", str(24 * 3600)))
# Skip the EDGAR fetch entirely in tests/CI: ``EDGAR_DISABLE_SCHEDULER=1``.
EDGAR_DISABLE_SCHEDULER = os.environ.get("EDGAR_DISABLE_SCHEDULER", "").strip() == "1"


async def _edgar_refresh_loop() -> None:
    """Run an initial sweep if the DB has no filings, then refresh daily.

    All work runs in a thread executor — the seeder uses blocking httpx +
    sqlite3 and we don't want to block the event loop.
    """
    try:
        import seed_13f  # type: ignore[import-not-found]
    except ImportError as e:
        log.warning("EDGAR seeder unavailable (%s) — refresh loop disabled", e)
        return

    loop = asyncio.get_running_loop()

    # Phase 1: seed once if empty. Runs on the first iteration of the loop
    # so a slow EDGAR doesn't delay server boot.
    try:
        with _db_lock:
            empty = seed_13f.filings_view_is_empty(_conn)
        if empty:
            log.info("filings tables empty — kicking initial EDGAR sweep")
            inserted = await loop.run_in_executor(None, seed_13f.main, DB_PATH)
            log.info("initial EDGAR sweep complete — %d rows inserted", inserted)
    except Exception as e:  # noqa: BLE001 — never let the loop die
        log.warning("initial EDGAR sweep failed: %s", e)

    # Phase 2: daily refresh forever.
    while True:
        try:
            await asyncio.sleep(EDGAR_REFRESH_SECONDS)
            log.info("starting scheduled EDGAR refresh")
            inserted = await loop.run_in_executor(None, seed_13f.main, DB_PATH)
            log.info("scheduled EDGAR refresh complete — %d rows inserted", inserted)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("scheduled EDGAR refresh failed: %s", e)


@app.on_event("startup")
async def _on_startup() -> None:
    _init_schema()
    _startup_vacuum()
    _seed_whales()
    if EDGAR_DISABLE_SCHEDULER:
        log.info("EDGAR_DISABLE_SCHEDULER=1 — refresh loop suppressed")
        return
    try:
        asyncio.create_task(_edgar_refresh_loop())
        log.info("EDGAR refresh loop scheduled (every %ds)", EDGAR_REFRESH_SECONDS)
    except RuntimeError as e:
        # No running event loop — startup outside a real ASGI server.
        log.warning("could not schedule EDGAR refresh loop: %s", e)


# ──────────────────────────────────────────────────────────────────────────────
# Sample data — until the EDGAR seeder runs, /api/recent-filings serves this.
# Stamp the sample with "now-ish" timestamps so the UI renders fresh-looking
# rows on a clean install.
# ──────────────────────────────────────────────────────────────────────────────

def _sample_recent_filings() -> list[dict]:
    now = int(time.time())
    day = 86400
    sample = [
        {
            "form": "SC 13D",
            "accession_no": "SAMPLE-13D-0001",
            "filer_cik": "0001336528",
            "filer_name": "Pershing Square Capital Management",
            "subject_ticker": "CMG",
            "subject_name": "Chipotle Mexican Grill",
            "event_date": "2026-05-12",
            "filed_at": now - 2 * day,
            "value_usd": None,
            "detail_count": 12500000,
            "summary": "Acquired 7.1% beneficial ownership; intend to engage management on capital allocation.",
            "raw_url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001336528",
        },
        {
            "form": "13F-HR",
            "accession_no": "SAMPLE-13F-0001",
            "filer_cik": "0001067983",
            "filer_name": "Berkshire Hathaway Inc",
            "subject_ticker": None,
            "subject_name": None,
            "event_date": "2026-03-31",
            "filed_at": now - 5 * day,
            "value_usd": 318_400_000_000.0,
            "detail_count": 41,
            "summary": None,
            "raw_url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001067983",
        },
        {
            "form": "Form 4",
            "accession_no": "SAMPLE-F4-0001",
            "filer_cik": "0001494730",
            "filer_name": "Elon Musk",
            "subject_ticker": "TSLA",
            "subject_name": "Tesla Inc",
            "event_date": "2026-05-13",
            "filed_at": now - 1 * day,
            "value_usd": -2_100_000_000.0,
            "detail_count": -5_000_000,
            "summary": "S",
            "raw_url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001494730",
        },
        {
            "form": "SC 13G",
            "accession_no": "SAMPLE-13G-0001",
            "filer_cik": "0001364742",
            "filer_name": "BlackRock Inc",
            "subject_ticker": "NVDA",
            "subject_name": "NVIDIA Corp",
            "event_date": "2026-05-08",
            "filed_at": now - 3 * day,
            "value_usd": None,
            "detail_count": 195_000_000,
            "summary": "Passive ownership crossed 7.9%.",
            "raw_url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001364742",
        },
        {
            "form": "SC 13D",
            "accession_no": "SAMPLE-13D-0002",
            "filer_cik": "0001167730",
            "filer_name": "Elliott Investment Management LP",
            "subject_ticker": "PYPL",
            "subject_name": "PayPal Holdings Inc",
            "event_date": "2026-05-09",
            "filed_at": now - 2 * day - 14400,
            "value_usd": None,
            "detail_count": 38_500_000,
            "summary": "Activist position; will push for accelerated buybacks and cost cuts.",
            "raw_url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001167730",
        },
    ]
    return sample


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "whale-dashboard", "ts": time.time()}


# ──────────────────────────────────────────────────────────────────────────────
# Sentry deploy-verification endpoint.
#
# Raises a deliberate exception so an operator can confirm the subproduct's
# Sentry DSN is wired correctly after a deploy. The gateway_auth HMAC
# middleware above already gates every request, but we add a second check
# here so a non-admin user with a valid session can't burn through Sentry
# quota. Two ways to pass:
#   1. NARVE_ADMIN_EMAIL set and matches the gateway-injected user email, OR
#   2. Request comes directly from loopback (no gateway in front — useful
#      for local debugging when DEV_MODE skips the HMAC check).
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/_sentry-test")
async def _sentry_test(request: Request) -> dict[str, Any]:
    admin_email = os.environ.get("NARVE_ADMIN_EMAIL", "").strip().lower()
    gw_email = request.headers.get("x-gateway-user-email", "").strip().lower()
    client_host = (request.client.host if request.client else "") or ""
    is_admin = bool(admin_email) and gw_email == admin_email
    is_local = client_host in ("127.0.0.1", "::1")
    if not (is_admin or is_local):
        raise HTTPException(status_code=403, detail="admin or loopback only")
    raise RuntimeError("Sentry test event — this is intentional (whale-dashboard)")


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    if HTML_PATH.exists():
        return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"))
    return HTMLResponse(
        "<html><body><h1>Whale Watch</h1><p>Frontend asset missing.</p></body></html>",
        status_code=200,
    )


# ── PWA: favicon + webmanifest ────────────────────────────────────────────────
# Browsers auto-hit /favicon.ico on every tab and request /manifest.webmanifest
# whenever the HTML <link rel="manifest"> resolves. Both point at the apex logo
# so the subdomain inherits narve.ai branding without bundling its own assets.
# Both paths are in _AUTH_BYPASS_EXACT so unauthenticated PWA install probes
# succeed (iOS Add-to-Home fetches the manifest before the user logs in).

@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=302, headers={"Location": "https://narve.ai/_gateway_static/img/logo.png"})


@app.get("/manifest.webmanifest")
def manifest() -> JSONResponse:
    return JSONResponse(
        {
            "name": "narve.ai — Whale Watch",
            "short_name": "Whale",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#ffffff",
            "theme_color": "#0d0d0d",
            "icons": [
                {"src": "https://narve.ai/_gateway_static/img/logo.png", "sizes": "256x256", "type": "image/png"}
            ],
        },
        media_type="application/manifest+json",
    )


@app.get("/api/whales")
def api_whales(
    kind: Optional[str] = Query(default=None, description="Filter by 'fund'|'activist'|'insider'|'family_office'"),
    active_only: bool = Query(default=True),
) -> JSONResponse:
    sql = "SELECT cik, name, short_name, kind, aum_usd_b, twitter, website, notes, is_active FROM whales WHERE 1=1"
    args: list[Any] = []
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    if active_only:
        sql += " AND is_active = 1"
    sql += " ORDER BY (aum_usd_b IS NULL), aum_usd_b DESC, name ASC"
    with _db_lock:
        rows = [dict(r) for r in _conn.execute(sql, args).fetchall()]
    return JSONResponse({"whales": rows, "count": len(rows)})


@app.get("/api/recent-filings")
def api_recent_filings(
    limit: int = Query(default=50, ge=1, le=500),
    form: Optional[str] = Query(default=None, description="13F-HR | SC 13D | SC 13G | Form 4"),
) -> JSONResponse:
    """Latest filings across types via the `filings_unified` view.

    If the DB has no rows yet (fresh install), fall back to sample data so
    the front-end isn't empty.
    """
    sql = "SELECT * FROM filings_unified"
    args: list[Any] = []
    if form:
        sql += " WHERE form = ?"
        args.append(form)
    sql += " ORDER BY filed_at DESC LIMIT ?"
    args.append(int(limit))
    with _db_lock:
        try:
            rows = [dict(r) for r in _conn.execute(sql, args).fetchall()]
        except sqlite3.Error as e:
            log.warning("filings_unified query failed: %s", e)
            rows = []
    if not rows:
        sample = _sample_recent_filings()
        if form:
            sample = [r for r in sample if r.get("form") == form]
        rows = sample[:limit]
        return JSONResponse({"filings": rows, "count": len(rows), "source": "sample"})
    return JSONResponse({"filings": rows, "count": len(rows), "source": "db"})


@app.get("/api/whales/{cik}/positions")
def api_whale_positions(cik: str, limit: int = Query(default=100, ge=1, le=1000)) -> JSONResponse:
    """Latest 13F positions for a single filer (most recent quarter)."""
    with _db_lock:
        whale = _conn.execute(
            "SELECT cik, name, short_name, kind, aum_usd_b FROM whales WHERE cik = ?", (cik,)
        ).fetchone()
        if not whale:
            raise HTTPException(status_code=404, detail="whale not found")
        latest = _conn.execute(
            "SELECT accession_no, period_of_report, filed_at, total_value_usd, n_positions "
            "FROM filings_13f WHERE cik = ? ORDER BY period_of_report DESC LIMIT 1",
            (cik,),
        ).fetchone()
        positions: list[dict] = []
        if latest:
            positions = [
                dict(r)
                for r in _conn.execute(
                    "SELECT cusip, ticker, issuer_name, shares, value_usd, pct_portfolio "
                    "FROM filings_13f_positions WHERE accession_no = ? "
                    "ORDER BY value_usd DESC LIMIT ?",
                    (latest["accession_no"], int(limit)),
                ).fetchall()
            ]
    return JSONResponse(
        {
            "whale": dict(whale),
            "latest_filing": dict(latest) if latest else None,
            "positions": positions,
            "count": len(positions),
        }
    )


@app.get("/api/consensus")
def api_consensus(
    min_whales: int = Query(default=3, ge=2, le=20),
    limit: int = Query(default=50, ge=1, le=500),
) -> JSONResponse:
    """Tickers held by >= `min_whales` distinct whales in their most recent 13F.

    Returns aggregate share count + total $ value across consenting whales.
    """
    sql = """
        WITH latest_per_whale AS (
            SELECT cik, MAX(period_of_report) AS period
              FROM filings_13f
             GROUP BY cik
        ),
        latest_accessions AS (
            SELECT f.accession_no, f.cik
              FROM filings_13f f
              JOIN latest_per_whale lpw
                ON lpw.cik = f.cik
               AND lpw.period = f.period_of_report
        )
        SELECT p.ticker,
               MAX(p.issuer_name)                    AS issuer_name,
               COUNT(DISTINCT la.cik)                AS whale_count,
               SUM(p.shares)                         AS total_shares,
               SUM(p.value_usd)                      AS total_value_usd,
               GROUP_CONCAT(DISTINCT w.short_name)   AS whales_short
          FROM filings_13f_positions p
          JOIN latest_accessions la ON la.accession_no = p.accession_no
          LEFT JOIN whales w        ON w.cik          = la.cik
         WHERE p.ticker IS NOT NULL
         GROUP BY p.ticker
        HAVING whale_count >= ?
         ORDER BY whale_count DESC, total_value_usd DESC
         LIMIT ?
    """
    with _db_lock:
        try:
            rows = [dict(r) for r in _conn.execute(sql, (int(min_whales), int(limit))).fetchall()]
        except sqlite3.Error as e:
            log.warning("consensus query failed: %s", e)
            rows = []
    return JSONResponse(
        {
            "consensus": rows,
            "count": len(rows),
            "min_whales": min_whales,
            "note": (
                "Empty until filings_13f_positions has been seeded. Run "
                "scripts/seed_13f.py against SEC EDGAR to populate."
                if not rows
                else None
            ),
        }
    )


@app.get("/api/watchlist")
def api_watchlist_get(request: Request) -> JSONResponse:
    u = _require_user(request)
    with _db_lock:
        rows = [
            dict(r)
            for r in _conn.execute(
                "SELECT kind, target, label, created_at FROM watchlist "
                "WHERE user_id = ? ORDER BY created_at DESC",
                (u["user_id"],),
            ).fetchall()
        ]
    return JSONResponse({"watchlist": rows, "count": len(rows)})


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8053"))
    # Loopback-only — the gateway is the sole ingress. Override with
    # ``BIND_HOST`` if you need to expose this directly for debugging.
    bind_host = os.environ.get("BIND_HOST", "127.0.0.1")
    log.info("Starting Whale Watch on %s:%d", bind_host, port)
    uvicorn.run("server:app", host=bind_host, port=port, log_level="info")

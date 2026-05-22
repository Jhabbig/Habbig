from __future__ import annotations
import asyncio
import hmac
import json
import logging
import os
import re
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import aiohttp
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Layered .env loader ──────────────────────────────────────────────────────
# See sports-dashboard for rationale. Walks ~/.gateway_env → gateway/.env.production
# → dashboard/.env.production → dashboard/.env, in priority order.
try:
    from dotenv import load_dotenv as _dotenv_load
except ImportError:
    def _dotenv_load(p, override=False):
        for raw in Path(p).read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if not override and k in os.environ:
                continue
            os.environ[k] = v
        return True
_DASHBOARD_DIR = Path(__file__).resolve().parent
_GATEWAY_ENV = None
for _p in [_DASHBOARD_DIR, *_DASHBOARD_DIR.parents][:5]:
    _candidate = _p / "gateway" / ".env.production"
    if _candidate.is_file():
        _GATEWAY_ENV = _candidate
        break
_ENV_SEARCH = [Path.home() / ".gateway_env"]
if _GATEWAY_ENV is not None:
    _ENV_SEARCH.append(_GATEWAY_ENV)
_ENV_SEARCH.extend([_DASHBOARD_DIR / ".env.production", _DASHBOARD_DIR / ".env"])
_loaded_env_files: list[str] = []
for _f in _ENV_SEARCH:
    if _f.is_file():
        _dotenv_load(_f, override=False)
        _loaded_env_files.append(str(_f))
print(f"[midterm-dashboard] env files loaded: {len(_loaded_env_files)}", flush=True)
for _f in _loaded_env_files:
    print(f"  ✓ {_f}", flush=True)
if not os.getenv("GATEWAY_SSO_SECRET"):
    print("⚠ [midterm-dashboard] GATEWAY_SSO_SECRET missing — gateway-fronted requests will be rejected", flush=True)

from database import Database
from aggregators import (
    PolymarketAggregator,
    KalshiAggregator,
    PredictItAggregator,
    PollingAggregator,
    ManifoldAggregator,
    MetaculusAggregator,
)
from cache import cache
from alerts import dispatch_divergence_alert
from smart_money import fetch_smart_money_flows, race_smart_money
from news import ingest_news, measure_reactions, lag_curve, tag_article
from election_night import assemble_election_night
from conditional import compute_conditional, joint_distribution_summary, apply_wave_swing
from calibration import calibration_table, calibration_over_time
import api_v1
from race_keys import parse_district_from_title, race_key_to_jurisdiction


def market_race_key(market: dict) -> str:
    """Build a canonical race_key for a market.

    For most race types this is ``{race_type}_{state}``. For US House races,
    we attempt to parse the district number from the title so each district
    becomes its own race (e.g. ``house_TX-28``).

    Markets that lack a meaningful race_type (missing or "other") or a state
    return a unique per-market sentinel keyed off ``source`` + ``source_id``.
    Without this, every unmatched market would collapse into the same
    ``other_US`` bucket — causing unrelated markets like "Bulgarian elections"
    and "Will LeBron be president" to be grouped as the same race.
    """
    rt_raw = market.get("race_type")
    st_raw = market.get("state")
    rt = (rt_raw or "").lower()
    st = (st_raw or "").upper()

    if not rt or rt == "other" or not st:
        # Cannot canonicalize — keep this market in its own bucket so the
        # divergence calculator and detail endpoint never collide it with
        # an unrelated market.
        return f"unmatched_{market.get('source', 'x')}_{market.get('source_id', '')}"

    if rt == "house":
        title = market.get("event_title") or market.get("title") or ""
        district = parse_district_from_title(title)
        if district:
            return f"house_{st}-{district}"
        # House market without a parseable district is ambiguous — don't
        # group it with the at-large state bucket.
        return f"unmatched_{market.get('source', 'x')}_{market.get('source_id', '')}"

    return f"{rt}_{st}"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("midterm-dashboard")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PORT = int(os.getenv("PORT", "8051"))
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
DATA_REFRESH_INTERVAL = 300  # 5 minutes
DIVERGENCE_INTERVAL = 300  # 5 minutes

# Rate-limit thresholds (requests per minute)
RATE_LIMITS = {"free": 60, "premium": 120, "admin": 0}  # 0 = unlimited

# Tier hierarchy for access checks
TIER_RANK = {"free": 0, "premium": 1, "admin": 2}

# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

class AlertBody(BaseModel):
    race_key: str
    threshold: Optional[float] = None
    direction: Optional[str] = "any"  # "up", "down", "any"


class FlagMarketBody(BaseModel):
    source: str
    source_id: str
    note: Optional[str] = None


class VerifyRaceBody(BaseModel):
    note: Optional[str] = None


# ---------------------------------------------------------------------------
# Shared application state (populated during lifespan)
# ---------------------------------------------------------------------------
class AppState:
    db: Database
    http_session: aiohttp.ClientSession
    polymarket: PolymarketAggregator
    kalshi: KalshiAggregator
    predictit: PredictItAggregator
    polling: PollingAggregator
    manifold: ManifoldAggregator
    metaculus: MetaculusAggregator

    def __init__(self):
        self.background_tasks: list[asyncio.Task] = []


state = AppState()

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
# Auth is now handled by the gateway. The gateway forwards authenticated
# requests with X-Gateway-Secret, X-Gateway-User-Id (UUID), and
# X-Gateway-User-Email headers.

async def require_auth(request: Request) -> dict:
    """Return the current user dict or raise 401.

    The gateway sets ``X-Gateway-User-Id`` (UUID string) and
    ``X-Gateway-User-Email`` after verifying the user's session +
    subscription. Trust is proved by a shared-secret header
    (``X-Gateway-Secret``).
    """
    import hmac
    _sso_secret = os.environ.get("GATEWAY_SSO_SECRET")
    _provided = request.headers.get("x-gateway-secret", "")
    if _sso_secret and hmac.compare_digest(_provided, _sso_secret):
        gw_id = request.headers.get("x-gateway-user-id")
        gw_email = request.headers.get("x-gateway-user-email")
        gw_tier = request.headers.get("x-gateway-user-tier", "free")
        gw_display = request.headers.get("x-gateway-user-display-name", "")
        if gw_id and gw_email:
            return {
                "id": gw_id,  # UUID string
                "email": gw_email,
                "tier": gw_tier,
                "display_name": gw_display or gw_email.split("@")[0],
            }

    raise HTTPException(status_code=401, detail="Not authenticated")


async def require_tier(request: Request, tier: str) -> dict:
    """Return the current user if their tier >= *tier*, else raise 403."""
    user = await require_auth(request)
    user_rank = TIER_RANK.get(user.get("tier", "free"), 0)
    required_rank = TIER_RANK.get(tier, 99)
    if user_rank < required_rank:
        raise HTTPException(status_code=403, detail=f"Requires {tier} tier or above")
    return user


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

def _check_rate_limit(identity: str, tier: str) -> bool:
    """Return True if the request is allowed, False if rate-limited.

    Backed by Redis when available so quotas are shared across uvicorn
    workers and survive restarts. Falls back to per-process in-memory
    counters when Redis is offline.
    """
    limit = RATE_LIMITS.get(tier, 60)
    if limit == 0:
        return True  # unlimited
    return cache.rate_limit_check(identity, limit=limit, window_seconds=60)


# ---------------------------------------------------------------------------
# Audit logging helper
# ---------------------------------------------------------------------------

async def _audit_log(action: str, user_id: Optional[str], ip: str, detail: str = ""):
    try:
        state.db.log_action(user_id=user_id, action=action, details=detail, ip=ip)
    except Exception as e:
        logger.warning(f"Audit log write failed: {e}")


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def data_refresh_loop():
    """Fetch market data from all aggregators every 5 minutes and store in DB."""
    while True:
        try:
            logger.info("Starting data refresh cycle")
            async def with_timeout(coro, name, seconds=60):
                try:
                    return await asyncio.wait_for(coro, timeout=seconds)
                except asyncio.TimeoutError:
                    logger.warning(f"{name} fetch timed out after {seconds}s")
                    raise asyncio.TimeoutError(f"{name} timed out")

            # Fetch sources in parallel (except Kalshi world which reuses Kalshi's cache)
            results = await asyncio.gather(
                with_timeout(state.polymarket.fetch_election_markets(), "Polymarket", seconds=120),
                with_timeout(state.kalshi.fetch_election_markets(), "Kalshi", seconds=180),
                with_timeout(state.predictit.fetch_election_markets(), "PredictIt"),
                with_timeout(state.polling.fetch_all_polls(), "Polling", seconds=30),
                with_timeout(state.polymarket.fetch_world_election_markets(), "Polymarket-World", seconds=120),
                with_timeout(state.manifold.fetch_election_markets(), "Manifold", seconds=60),
                with_timeout(state.manifold.fetch_world_election_markets(), "Manifold-World", seconds=60),
                with_timeout(state.metaculus.fetch_election_markets(), "Metaculus", seconds=60),
                with_timeout(state.metaculus.fetch_world_election_markets(), "Metaculus-World", seconds=60),
                return_exceptions=True,
            )
            (
                poly_data, kalshi_data, pi_data, poll_data, poly_world,
                manifold_data, manifold_world,
                metaculus_data, metaculus_world,
            ) = results

            # Kalshi world uses cached data from the midterm fetch above
            try:
                kalshi_world = await with_timeout(
                    state.kalshi.fetch_world_election_markets(), "Kalshi-World", seconds=30
                )
            except Exception as e:
                logger.error(f"Kalshi-World fetch error: {e}")
                kalshi_world = e

            # Store midterm markets
            for label, data in [
                ("Polymarket", poly_data),
                ("Kalshi", kalshi_data),
                ("PredictIt", pi_data),
                ("Manifold", manifold_data),
                ("Metaculus", metaculus_data),
            ]:
                if isinstance(data, list):
                    state.db.upsert_markets_batch(data)
                    logger.info(f"Stored {len(data)} {label} markets")
                else:
                    logger.error(f"{label} fetch error: {data}")

            # Store polls
            if isinstance(poll_data, dict):
                all_polls = []
                for poll_type, polls in poll_data.items():
                    all_polls.extend(polls)
                if all_polls:
                    state.db.store_polls_batch(all_polls)
                logger.info(f"Stored {len(all_polls)} polls")
            else:
                logger.error(f"Polling fetch error: {poll_data}")

            # Store world election markets
            for label, data in [
                ("Polymarket world", poly_world),
                ("Kalshi world", kalshi_world),
                ("Manifold world", manifold_world),
                ("Metaculus world", metaculus_world),
            ]:
                if isinstance(data, list):
                    state.db.upsert_markets_batch(data)
                    logger.info(f"Stored {len(data)} {label} markets")
                else:
                    logger.error(f"{label} fetch error: {data}")

            # Snapshot the current top-outcome prices into midterm_price_history.
            # This is what the news-reaction measurer joins against to compute
            # market response to events. Without it, the price-history table
            # stays empty and lag measurement is impossible.
            all_market_payloads: list[dict] = []
            for data in (poly_data, kalshi_data, pi_data, manifold_data, metaculus_data,
                         poly_world, kalshi_world, manifold_world, metaculus_world):
                if isinstance(data, list):
                    all_market_payloads.extend(data)
            snap_count = state.db.record_price_snapshots_for_markets(all_market_payloads)
            if snap_count:
                logger.info(f"Recorded {snap_count} price snapshots")

            # Notify connected SSE clients that fresh data has landed.
            cache.publish("data_updated", {"phase": "markets"})

        except Exception as e:
            logger.error(f"Data refresh error: {e}", exc_info=True)

        await asyncio.sleep(DATA_REFRESH_INTERVAL)


async def divergence_calculator():
    """Compute divergence across sources for matched races every 5 minutes."""
    while True:
        try:
            logger.info("Computing divergence snapshots")
            all_markets = state.db.get_all_markets(active_only=True)
            # Fetch human-review flags once per pass so we don't hit SQLite
            # per-market inside the grouping loop.
            wrong_flags = state.db.get_all_wrong_flags()

            # Group markets by race_key (race_type + state, with district for house)
            by_race: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
            for m in all_markets:
                race_key = market_race_key(m)
                # Skip markets a human flagged as "wrong" for this race.
                if (m.get("source", ""), m.get("source_id", "")) in wrong_flags.get(race_key, set()):
                    continue
                source = m.get("source", "unknown")
                by_race[race_key][source].append(m)

            count = 0
            for race_key, sources in by_race.items():
                # Unmatched markets get a unique per-source sentinel from
                # market_race_key — never compare them across sources.
                if race_key.startswith("unmatched_"):
                    continue
                source_probs: dict[str, float] = {}
                for source, markets in sources.items():
                    # Use the first market's top outcome probability
                    for market in markets:
                        outcomes = market.get("outcomes", [])
                        if outcomes and outcomes[0].get("probability") is not None:
                            source_probs[source] = outcomes[0]["probability"]
                            break

                if len(source_probs) < 1:
                    continue

                values = list(source_probs.values())
                max_div = (max(values) - min(values)) if len(values) >= 2 else 0.0

                parts = race_key.split("_", 1)
                race_type = parts[0] if parts else "other"
                state_abbr = parts[1] if len(parts) > 1 else None
                # House district keys look like "TX-28"; strip district for the state column
                if race_type == "house" and state_abbr and "-" in state_abbr:
                    state_abbr = state_abbr.split("-", 1)[0]

                state.db.record_divergence(
                    race_key=race_key,
                    state=state_abbr,
                    race_type=race_type,
                    data={
                        "polymarket": source_probs.get("polymarket"),
                        "kalshi": source_probs.get("kalshi"),
                        "predictit": source_probs.get("predictit"),
                        "polling": source_probs.get("polling"),
                        # New sources are folded into details and remain
                        # readable to clients via source_probs.
                        "manifold": source_probs.get("manifold"),
                        "metaculus": source_probs.get("metaculus"),
                        "max_divergence": round(max_div, 4),
                        "details": source_probs,
                    }
                )
                count += 1

            logger.info(f"Divergence calculated for {count} races")
            cache.publish("data_updated", {"phase": "divergence", "races": count})
        except Exception as e:
            logger.error(f"Divergence calculator error: {e}", exc_info=True)

        await asyncio.sleep(DIVERGENCE_INTERVAL)


def _seed_district_profiles():
    """Load static state profiles into the DB on startup."""
    from district_profiles import get_all_profiles
    profiles = get_all_profiles()
    existing = state.db.get_profiled_states()
    count = 0
    for abbr, profile in profiles.items():
        if abbr not in existing:
            state.db.upsert_district_profile(
                state=abbr,
                name=profile.get("name", abbr),
                profile_data=profile,
                auto_generated=False,
            )
            count += 1
        else:
            # Update existing profiles with latest static data
            state.db.upsert_district_profile(
                state=abbr,
                name=profile.get("name", abbr),
                profile_data=profile,
                auto_generated=False,
            )
    logger.info(f"Seeded {count} new district profiles, updated {len(profiles) - count} existing")


async def district_profile_updater():
    """Background task: scan active races for states without profiles and generate them."""
    PROFILE_CHECK_INTERVAL = 3600  # 1 hour
    while True:
        try:
            from district_profiles import get_profile, generate_basic_profile

            all_markets = state.db.get_all_markets(active_only=True)
            existing = state.db.get_profiled_states()

            # Collect all states from active races
            race_states: set[str] = set()
            for m in all_markets:
                st = m.get("state")
                if st and st != "US" and len(st) <= 3:
                    race_states.add(st.upper())

            new_count = 0
            for st in race_states:
                if st in existing:
                    continue

                # Try static data first, fall back to auto-generated template
                profile = get_profile(st)
                if profile:
                    state.db.upsert_district_profile(
                        state=st,
                        name=profile.get("name", st),
                        profile_data=profile,
                        auto_generated=False,
                    )
                else:
                    placeholder = generate_basic_profile(st)
                    state.db.upsert_district_profile(
                        state=st,
                        name=placeholder.get("name", st),
                        profile_data=placeholder,
                        auto_generated=True,
                    )
                new_count += 1

            if new_count:
                logger.info(f"District profile updater: created {new_count} new profiles")

        except Exception as e:
            logger.error(f"District profile updater error: {e}", exc_info=True)

        await asyncio.sleep(PROFILE_CHECK_INTERVAL)


async def news_ingest_loop():
    """Fetch + tag political news every 5 minutes; measure reactions every minute."""
    NEWS_FETCH_INTERVAL = 300
    while True:
        try:
            added = await ingest_news(state.db)
            if added:
                cache.publish("data_updated", {"phase": "news", "new_items": added})
        except Exception as e:
            logger.error(f"News ingest error: {e}", exc_info=True)
        await asyncio.sleep(NEWS_FETCH_INTERVAL)


async def news_reaction_loop():
    """Compute market reactions for tagged news events.

    Runs more often than the ingest loop because reactions get measurable as
    price snapshots accumulate after each new piece of news.
    """
    REACTION_INTERVAL = 60
    while True:
        try:
            written = await measure_reactions(state.db)
            if written:
                cache.publish("data_updated", {"phase": "news_reactions", "rows": written})
        except Exception as e:
            logger.error(f"News reaction error: {e}", exc_info=True)
        await asyncio.sleep(REACTION_INTERVAL)


async def alert_dispatch_loop():
    """Watch divergence snapshots and dispatch alerts for breached thresholds.

    Runs every minute. For each enabled alert we look up the latest divergence
    snapshot for the watched race, compare to the user's threshold, and
    dispatch via email + Telegram if (a) the threshold is breached and (b)
    we haven't already sent the same alert in the last hour.

    The cooldown is intentionally simple — anything more sophisticated (e.g.
    "only re-alert when divergence crosses the threshold from below") would
    require state machines we haven't built yet.
    """
    INTERVAL_SECONDS = 60
    COOLDOWN_SECONDS = 3600  # 1 hour
    while True:
        try:
            alerts = state.db.get_all_active_alerts()
            sent = 0
            for a in alerts:
                race_key = a.get("race_key")
                if not race_key:
                    continue
                threshold = float(a.get("threshold") or 5.0) / 100.0  # stored as pp (5.0 = 5pp)
                snap = state.db.get_latest_divergence(race_key)
                if not snap:
                    continue
                max_div = float(snap.get("max_divergence") or 0)
                if max_div < threshold:
                    continue

                # Cooldown
                last = state.db.last_alert_time(a["user_id"], race_key, a.get("alert_type") or "divergence")
                if last:
                    try:
                        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                        if (datetime.now(timezone.utc) - last_dt).total_seconds() < COOLDOWN_SECONDS:
                            continue
                    except (ValueError, TypeError):
                        pass

                details = snap.get("divergence_details") or {}
                if isinstance(details, str):
                    try:
                        details = json.loads(details)
                    except json.JSONDecodeError:
                        details = {}

                result = await dispatch_divergence_alert(
                    user_email=a.get("email"),
                    user_telegram_chat=None,  # no per-user TG yet — env-default chat used
                    race_key=race_key,
                    threshold=threshold * 100.0,
                    max_div=max_div,
                    sources=details,
                )
                if result.get("email") or result.get("telegram"):
                    state.db.record_alert_dispatch(
                        user_id=a["user_id"],
                        race_key=race_key,
                        alert_type=a.get("alert_type") or "divergence",
                        message=f"max_div={max_div:.4f} threshold={threshold:.4f}",
                    )
                    sent += 1
            if sent:
                logger.info(f"Alert dispatcher: sent {sent} alerts")
        except Exception as e:
            logger.error(f"Alert dispatcher error: {e}", exc_info=True)
        await asyncio.sleep(INTERVAL_SECONDS)


async def db_retention_loop():
    """Daily sweep: prune old price-history + divergence rows.

    The price-history table grows unbounded if nothing trims it (the production
    DB was already 108MB+). Retention keeps 30 days of price points and 90
    days of divergence snapshots, then runs VACUUM weekly to reclaim space.
    """
    PRICE_RETAIN_DAYS = int(os.getenv("PRICE_RETAIN_DAYS", "30"))
    DIVERGENCE_RETAIN_DAYS = int(os.getenv("DIVERGENCE_RETAIN_DAYS", "90"))
    SWEEP_INTERVAL = 24 * 3600  # daily
    runs = 0
    while True:
        try:
            deleted_prices = state.db.prune_price_history(retain_days=PRICE_RETAIN_DAYS)
            deleted_div = state.db.prune_divergence_snapshots(retain_days=DIVERGENCE_RETAIN_DAYS)
            logger.info(
                f"DB retention: pruned {deleted_prices} price-history rows, "
                f"{deleted_div} divergence rows"
            )
            runs += 1
            # VACUUM is expensive (rewrites the whole file) — run weekly.
            if runs % 7 == 0:
                state.db.vacuum()
                logger.info("DB retention: VACUUM complete")
        except Exception as e:
            logger.error(f"DB retention error: {e}", exc_info=True)
        await asyncio.sleep(SWEEP_INTERVAL)


async def jurisdiction_profile_updater():
    """Background task: enrich every jurisdiction (state, district, country) with live data.

    Walks active markets, derives the (jurisdiction_type, jurisdiction_code) for each,
    and refreshes them via Census/BEA/BLS/World Bank/Wikipedia. Profiles older than
    7 days are refreshed; new ones are created on first sight. Runs hourly.
    """
    JURISDICTION_REFRESH_INTERVAL = 3600  # 1 hour
    STALE_AFTER_DAYS = 7

    while True:
        try:
            from data_sources.enrich import (
                enrich_state_profile,
                enrich_house_district_profile,
                enrich_country_profile,
            )
            from district_profiles import get_profile as get_static_state
            from datetime import datetime, timezone, timedelta

            all_markets = state.db.get_all_markets(active_only=True)

            # Build the unique set of jurisdictions referenced by active markets
            jurisdictions: set[tuple[str, str]] = set()
            for m in all_markets:
                rt = (m.get("race_type") or "").lower()
                st = (m.get("state") or "").upper()
                title = m.get("event_title") or m.get("title") or ""

                if rt == "world" and st:
                    jurisdictions.add(("country", st))
                elif rt == "house" and st and st != "US":
                    district = parse_district_from_title(title)
                    if district:
                        jurisdictions.add(("us_district", f"{st}-{district}"))
                    else:
                        jurisdictions.add(("us_state", st))
                elif st and st != "US" and len(st) <= 3:
                    jurisdictions.add(("us_state", st))

            stale_cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_AFTER_DAYS)
            refreshed = 0
            errors = 0

            for jt, jc in jurisdictions:
                # Skip if cached and fresh
                cached = state.db.get_jurisdiction_profile(jt, jc)
                if cached:
                    updated_at = cached.get("updated_at") or ""
                    try:
                        ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                        if ts > stale_cutoff:
                            continue
                    except (ValueError, TypeError):
                        pass

                try:
                    if jt == "us_state":
                        base = get_static_state(jc) or {}
                        enriched = await enrich_state_profile(state.http_session, jc, base=base)
                        name = enriched.get("name") or base.get("name") or jc
                    elif jt == "us_district":
                        st_code, district = jc.split("-", 1)
                        enriched = await enrich_house_district_profile(
                            state.http_session, st_code, district
                        )
                        name = f"{st_code} District {int(district)}" if district != "00" else f"{st_code} (At-large)"
                    elif jt == "country":
                        enriched = await enrich_country_profile(state.http_session, jc)
                        name = enriched.get("name") or jc
                    else:
                        continue

                    state.db.upsert_jurisdiction_profile(
                        jurisdiction_type=jt,
                        jurisdiction_code=jc,
                        name=name,
                        profile_data=enriched,
                        auto_generated=True,
                    )
                    refreshed += 1
                except Exception as e:
                    errors += 1
                    logger.warning(f"Jurisdiction enrich failed for {jt}/{jc}: {e}")

            if refreshed or errors:
                logger.info(
                    f"Jurisdiction profile updater: refreshed {refreshed}, errors {errors}, "
                    f"total tracked {len(jurisdictions)}"
                )

        except Exception as e:
            logger.error(f"Jurisdiction profile updater error: {e}", exc_info=True)

        await asyncio.sleep(JURISDICTION_REFRESH_INTERVAL)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing application")
    state.db = Database()
    state.db.connect()

    # Connect to Redis. If unavailable the dashboard still runs — rate-limit
    # quotas just become per-process and SSE clients get an offline notice.
    cache.connect()

    state.http_session = aiohttp.ClientSession()

    state.polymarket = PolymarketAggregator(session=state.http_session)
    state.kalshi = KalshiAggregator(session=state.http_session)
    state.predictit = PredictItAggregator(session=state.http_session)
    state.polling = PollingAggregator(session=state.http_session)
    state.manifold = ManifoldAggregator(session=state.http_session)
    state.metaculus = MetaculusAggregator(session=state.http_session)

    # Seed district profiles from static data on startup
    _seed_district_profiles()

    # Start background tasks
    state.background_tasks = [
        asyncio.create_task(data_refresh_loop(), name="data_refresh"),
        asyncio.create_task(divergence_calculator(), name="divergence"),
        asyncio.create_task(district_profile_updater(), name="district_profiles"),
        asyncio.create_task(jurisdiction_profile_updater(), name="jurisdiction_profiles"),
        asyncio.create_task(db_retention_loop(), name="db_retention"),
        asyncio.create_task(alert_dispatch_loop(), name="alert_dispatch"),
        asyncio.create_task(news_ingest_loop(), name="news_ingest"),
        asyncio.create_task(news_reaction_loop(), name="news_reactions"),
    ]

    logger.info("Background tasks started")
    yield

    # Shutdown
    logger.info("Shutting down")
    for task in state.background_tasks:
        task.cancel()
    await asyncio.gather(*state.background_tasks, return_exceptions=True)

    await state.http_session.close()
    state.db.close()
    logger.info("Shutdown complete")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Midterm Elections Dashboard",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
)


# Paths that are intended to be consumed by third parties — open CORS and
# allow framing so journalists / partners can embed and call the API.
PUBLIC_API_PREFIXES = ("/v1/", "/embed/")


@app.middleware("http")
async def public_cors_middleware(request: Request, call_next):
    """Permissive CORS for ``/v1/*`` and ``/embed/*``. Preflights short-circuit
    here so they don't fall through to the credentialed CORS middleware."""
    path = request.url.path
    is_public = path.startswith(PUBLIC_API_PREFIXES)
    if is_public and request.method == "OPTIONS":
        return JSONResponse(
            status_code=204,
            content=None,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
                "Access-Control-Max-Age": "86400",
            },
        )
    response = await call_next(request)
    if is_public:
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Expose-Headers"] = "X-narve-version"
        response.headers["X-narve-version"] = "v1"
    return response


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Embed routes intentionally allow framing so iframes work cross-origin;
    # everything else stays locked down with DENY.
    is_embed = request.url.path.startswith("/embed/")
    if not is_embed:
        response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if is_embed:
        # SPA needs inline-style budget for embedded charts; framing allowed
        # because the route exists to be iframed.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors *"
        )
    else:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'"
        )
    return response


@app.middleware("http")
async def csrf_xhr_middleware(request: Request, call_next):
    """Require X-Requested-With: XMLHttpRequest on state-changing endpoints."""
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        path = request.url.path
        csrf_paths = ("/premium/watchlist", "/premium/alerts", "/admin/user/")
        if any(path.startswith(p) for p in csrf_paths):
            xhr = request.headers.get("x-requested-with", "")
            if xhr != "XMLHttpRequest":
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Missing required X-Requested-With header"},
                )
    return await call_next(request)


def _gateway_authenticated(request: Request) -> bool:
    """Return True if the request carries a valid gateway HMAC header."""
    secret = os.environ.get("GATEWAY_SSO_SECRET")
    if not secret:
        return False
    provided = request.headers.get("x-gateway-secret", "")
    return bool(provided) and hmac.compare_digest(provided, secret)


def _client_identity(request: Request) -> str:
    """Identify the caller for rate-limiting / auditing.

    Without this helper every request looked like it came from the gateway's
    IP because the dashboard always sees `request.client.host == <gateway>`.
    A globally-shared rate quota was the result. We now:

    1. Prefer the authenticated user id if the gateway forwarded one and the
       gateway HMAC header validates (so the id is trustworthy).
    2. Otherwise fall back to the first hop in X-Forwarded-For — but only when
       the gateway secret is present, because that header is trivially
       spoofable on a direct connection.
    3. Else fall back to the raw socket peer.
    """
    if _gateway_authenticated(request):
        uid = request.headers.get("x-gateway-user-id", "").strip()
        if uid:
            return f"user:{uid}"
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return f"ip:{first}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    # Only trust the tier header if the gateway secret is valid
    if _gateway_authenticated(request):
        tier = request.headers.get("x-gateway-user-tier", "free")
    else:
        tier = "free"

    identity = _client_identity(request)

    if not _check_rate_limit(identity, tier):
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Please slow down."},
        )

    return await call_next(request)


@app.middleware("http")
async def audit_sensitive_actions(request: Request, call_next):
    """Log sensitive actions (admin, premium) to the audit log."""
    response = await call_next(request)

    path = request.url.path
    if path.startswith(("/admin/", "/premium/")):
        identity = _client_identity(request)
        # Only trust the gateway-supplied user id when the HMAC header proves
        # the request actually came through the gateway. Otherwise an attacker
        # hitting the backend directly could poison the audit log with any
        # user id they liked.
        if _gateway_authenticated(request):
            user_id = request.headers.get("x-gateway-user-id")
        else:
            user_id = None
        await _audit_log(
            action=f"{request.method} {path}",
            user_id=user_id,
            ip=identity,
            detail=f"status={response.status_code}",
        )

    return response


# ===================================================================
# AUTH ENDPOINTS
# ===================================================================
# Registration, login, and logout are handled by the gateway.
# These endpoints redirect to the gateway for those actions.

@app.post("/auth/logout")
async def auth_logout(request: Request):
    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({"status": "ok"})
    return RedirectResponse("https://narve.ai/logout", status_code=302)


@app.get("/auth/me")
async def auth_me(request: Request):
    user = await require_auth(request)
    return {
        "id": user["id"],
        "email": user["email"],
        "display_name": user.get("display_name", ""),
        "tier": user.get("tier", "free"),
    }


# ===================================================================
# SERVER-SENT EVENTS
# ===================================================================

def _format_sse(event: str, data: dict) -> str:
    payload = json.dumps(data, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


@app.get("/data/stream")
async def data_stream(request: Request):
    """SSE stream of live data-update events for connected browsers.

    Subscribes to the same Redis ``dashboard:midterm`` channel the gateway
    listens on. Without Redis the endpoint sends one ``offline`` frame and
    closes — the frontend then falls back to its 5-min polling.
    """
    async def event_gen():
        try:
            async for msg in cache.subscribe_async():
                if await request.is_disconnected():
                    break
                event = msg.get("event", "data_updated")
                yield _format_sse(event, msg)
        except asyncio.CancelledError:
            raise

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(event_gen(), media_type="text/event-stream", headers=headers)


# ===================================================================
# PUBLIC DATA ENDPOINTS
# ===================================================================

_DEM_NAME_RE = re.compile(r"\b(democrat|dems?|democratic|d\.?)\b", re.I)
_REP_NAME_RE = re.compile(r"\b(republican|reps?|gop|r\.?)\b", re.I)


def _classify_outcome_party(outcome_name: str, market_title: str) -> Optional[str]:
    """Return ``"democrat"`` or ``"republican"`` for a control-market outcome.

    Recognised forms:
      * Outcome names that name a party directly ("Democratic", "GOP", etc.).
      * Yes/No outcomes when the title asks "will democrats..." / "will republicans...".

    Returns ``None`` if the outcome can't be classified — this is preferable
    to silently miscounting it.
    """
    name = (outcome_name or "").strip().lower()
    title = (market_title or "").lower()

    if _DEM_NAME_RE.search(name):
        return "democrat"
    if _REP_NAME_RE.search(name):
        return "republican"

    if name in {"yes", "no"}:
        # "Will Democrats win the Senate?" → Yes = dem
        if "democrat" in title:
            return "democrat" if name == "yes" else "republican"
        if "republican" in title or "gop" in title:
            return "republican" if name == "yes" else "democrat"
    return None


@app.get("/data/overview")
async def data_overview():
    """Senate/House control probabilities from all sources."""
    all_markets = state.db.get_all_markets(active_only=True)

    # Build control overview from "control" type markets
    senate_sources: dict[str, dict[str, float]] = {}
    house_sources: dict[str, dict[str, float]] = {}
    source_summary = defaultdict(lambda: {"market_count": 0, "status": "ok"})

    for m in all_markets:
        source = m.get("source", "unknown")
        source_summary[source]["market_count"] += 1

        if m.get("race_type") != "control":
            continue
        title = (m.get("title") or "")
        title_lower = title.lower()
        outcomes = m.get("outcomes", []) or []

        target = None
        if "senate" in title_lower:
            target = senate_sources
        elif "house" in title_lower:
            target = house_sources
        if target is None:
            continue

        # Tally probabilities by classified party. Markets often have multiple
        # outcomes (e.g. seat-count brackets); sum the dem vs rep probability
        # mass and only fall back to (1 - dem) when there's no rep classification.
        tallies = {"democrat": 0.0, "republican": 0.0}
        unclassified = 0
        for o in outcomes:
            prob = o.get("probability")
            if prob is None:
                continue
            party = _classify_outcome_party(o.get("name") or "", title)
            if party is None:
                unclassified += 1
                continue
            tallies[party] += float(prob)

        dem_prob = tallies["democrat"]
        rep_prob = tallies["republican"]
        if dem_prob and not rep_prob and unclassified == 0:
            rep_prob = max(0.0, 1.0 - dem_prob)
        if rep_prob and not dem_prob and unclassified == 0:
            dem_prob = max(0.0, 1.0 - rep_prob)

        if dem_prob == 0 and rep_prob == 0:
            continue
        target[source] = {
            "democrat": round(min(dem_prob, 1.0), 4),
            "republican": round(min(rep_prob, 1.0), 4),
        }

    return {
        "senate_control": {"sources": senate_sources},
        "house_control": {"sources": house_sources},
        "source_summary": dict(source_summary),
    }


def _canonical_question(m: dict) -> str:
    """Derive a canonical question key so the *same* question matches across
    sources while *different* questions stay separate.

    Examples:
      "Which party will win the 2026 US Senate election in Texas?"
        → senate_TX_party_winner
      "Who will win the 2026 Texas Republican Senate nomination?"
        → senate_TX_primary
      "Will Democrats win the U.S. Senate in 2026?"
        → senate_US_party_winner
      "Trump out as President before 2027?"
        → national_trump_leaves_office
      "Will China invade Taiwan by end of 2026?"
        → national_china_taiwan
    """
    title = (m.get("title") or "").lower()
    event = (m.get("event_title") or "").lower()
    text = title + " " + event
    rt = m.get("race_type", "other")
    st = m.get("state")

    # ---- state-level elections ----
    if st:
        if any(kw in text for kw in ("primary", "nomination", "nominee", "advance from")):
            # Distinguish Dem vs Rep primaries
            if any(kw in text for kw in ("republican", "gop", "rep ")):
                return f"{rt}_{st}_primary_r"
            elif any(kw in text for kw in ("democrat", "dem ", "democratic")):
                return f"{rt}_{st}_primary_d"
            return f"{rt}_{st}_primary"
        if any(kw in text for kw in ("which party", "will democrat", "will republican",
                                      "party win", "party control", "dems win",
                                      "republicans win", "democrats win")):
            return f"{rt}_{st}_party_winner"
        if "margin" in text:
            return f"{rt}_{st}_margin"
        if any(kw in text for kw in ("who will win", "election winner", "win the")):
            return f"{rt}_{st}_winner"
        # fallback: election-level grouping
        return f"{rt}_{st}_general"

    # ---- national / no-state markets ----
    # Senate control
    if any(kw in text for kw in ("control the senate", "win the senate",
                                  "party will control the senate",
                                  "senate in 2026", "win the u.s. senate")):
        return "national_senate_control"
    # House control
    if any(kw in text for kw in ("control the house", "win the house",
                                  "house in the 2026", "house seats")):
        return "national_house_control"
    # Balance of power
    if "balance of power" in text:
        return "national_balance_of_power"
    # Senate seat count
    if re.search(r"hold exactly \d+ senate", text) or "senate seats" in text:
        return "national_senate_seats"
    # Trump leaving office
    if "trump" in text and any(kw in text for kw in ("out as president", "removed",
                                                      "leave office", "resign")):
        return "national_trump_leaves"
    # Trump impeachment
    if "trump" in text and "impeach" in text:
        return "national_trump_impeach"
    # Speaker of the House
    if "speaker" in text:
        return "national_house_speaker"
    # White House press secretary
    if "press secretary" in text:
        return "national_press_secretary"
    # Senate leader
    if "senate" in text and ("leader" in text or "leadership" in text):
        return "national_senate_leader"
    # Governor seat count
    if "governor" in text and ("exactly" in text or "governorship" in text):
        return "national_governor_count"
    # Geopolitical / non-election (should not cross-match)
    for subject in ("china", "taiwan", "iran", "israel", "ukraine", "russia",
                     "greenland", "paramount", "caruso", "zelenskyy", "putin"):
        if subject in text:
            return f"geo_{subject}_{m.get('source', 'x')}_{m.get('source_id', '')}"

    # Fallback: unique per market to prevent false grouping
    return f"other_{m.get('source', 'x')}_{m.get('source_id', '')}"


@app.get("/data/races")
async def data_races(
    race_type: Optional[str] = None,
    state_abbr: Optional[str] = None,
    source: Optional[str] = None,
    search: Optional[str] = None,
    min_volume: Optional[float] = None,
):
    """List all tracked races with latest odds.

    Returns ``matched`` (elections with 2+ sources) and ``unmatched``
    (single-source elections) so the frontend can display them separately.
    Markets are grouped by canonical question so only truly equivalent
    questions are compared across sources.
    """
    markets = state.db.get_markets(
        race_type=race_type, state=state_abbr, source=source,
        search=search, min_volume=min_volume,
    )
    valid_race_types = {"senate", "house", "governor", "control", "presidential"}
    if race_type and race_type != "world":
        pass
    else:
        markets = [m for m in markets if m.get("race_type") in valid_race_types]

    # Fetch human-review state once per request.
    wrong_flags = state.db.get_all_wrong_flags()
    verifications = state.db.get_all_verifications()

    # --- group by canonical question ------------------------------------
    from collections import defaultdict
    buckets: dict[str, dict[str, list[dict]]] = defaultdict(
        lambda: defaultdict(list)
    )

    # Shallow-copy each row before mutating. The rows come straight from the
    # aggregator/db cache; mutating them in place leaks "market_id" and "_cq"
    # back into the cache and corrupts subsequent requests (and races with
    # background refresh tasks).
    for raw in markets:
        m = dict(raw)
        src = m.get("source", "unknown")
        sid = m.get("source_id", "")
        m["market_id"] = f"{src}_{sid}"
        cq = _canonical_question(m)
        mk = market_race_key(m)
        m["_cq"] = cq
        # Skip markets a human flagged as "wrong" for this race. The flag
        # could have been written under either the canonical question (cq)
        # or the market_race_key (mk) depending on which endpoint the admin
        # used — check both so the listing stays in sync with the detail page.
        pair = (src, sid)
        if pair in wrong_flags.get(cq, set()) or pair in wrong_flags.get(mk, set()):
            continue
        buckets[cq][src].append(m)

    matched = []
    unmatched = []

    for cq, by_source in buckets.items():
        # Pick the highest-volume market per source
        best = {}
        total_vol = 0
        for src, src_markets in by_source.items():
            rep = max(src_markets, key=lambda x: x.get("volume") or 0)
            best[src] = rep
            total_vol += rep.get("volume") or 0

        if not best:
            continue

        first = next(iter(best.values()))
        rt = first.get("race_type", "other")
        st = first.get("state")
        race_key = market_race_key(first) if st else cq

        # Derive district code (e.g. "28") for house races so the frontend can display it
        district = None
        if rt == "house" and "-" in (race_key or ""):
            district = race_key.split("-", 1)[1]

        for m in best.values():
            m["race_key"] = race_key

        # A race is human-verified when EITHER its canonical question
        # (``cq``) or its race_key has a verification record. We check both
        # so old verifications bookmarked against race_key keep working.
        verification = verifications.get(cq) or verifications.get(race_key)

        entry = {
            "race_key": race_key,
            "canonical": cq,
            "race_type": rt,
            "state": st,
            "district": district,
            "title": first.get("event_title") or first.get("title"),
            "sources": best,
            "source_count": len(best),
            "volume": total_vol,
            "verified": bool(verification),
            "verified_by": (verification or {}).get("reviewer_email"),
            "verified_at": (verification or {}).get("verified_at"),
        }

        if len(best) >= 2:
            matched.append(entry)
        else:
            unmatched.append(entry)

    matched.sort(key=lambda e: e["volume"], reverse=True)
    unmatched.sort(key=lambda e: e["volume"], reverse=True)

    return {"matched": matched, "unmatched": unmatched}


@app.get("/data/race/{race_key}")
async def data_race_detail(race_key: str):
    """Single race detail with all source data.

    race_key can be:
    - "race_type_STATE" e.g. "senate_GA" — matches all sources for that race
    - "house_STATE-NN" e.g. "house_TX-28" — district-specific house race
    - "world_CC" e.g. "world_HU" — international race
    - "source_sourceId" e.g. "predictit_8156" — direct market lookup, then find siblings
    - legacy "race_type_STATE_sourceId" format
    """
    # Length guard against DoS via absurdly long keys
    if len(race_key) > 200:
        return JSONResponse({"error": "invalid race_key"}, status_code=400)
    all_markets = state.db.get_all_markets(active_only=True)
    # Load flag state once so step 2 can skip flagged sibling markets.
    wrong_flags = state.db.get_all_wrong_flags()

    # Step 1: find the target market(s) using the canonical race_key helper
    matched = {}
    target_race_type = None
    target_state = None
    target_district = None  # for house races

    for m in all_markets:
        rt = m.get("race_type", "other")
        st = m.get("state") or "US"
        sid = m.get("source_id", "")
        source = m.get("source", "unknown")
        group_key = market_race_key(m)

        if (group_key == race_key
            or f"{source}_{sid}" == race_key
            or f"{rt}_{st}_{sid}" == race_key
            or sid == race_key):
            matched[source] = m
            target_race_type = rt
            target_state = st
            if rt == "house" and "-" in group_key:
                target_district = group_key.split("-", 1)[1]

    # Step 2: if we found a target, grab all sibling markets with the same canonical key
    if target_race_type and target_state:
        target_key = race_key if "_" in race_key else f"{target_race_type}_{target_state}"
        for m in all_markets:
            source = m.get("source", "unknown")
            if source in matched:
                continue
            if market_race_key(m) == target_key:
                # Don't re-attach a sibling the reviewer flagged as wrong.
                if (source, m.get("source_id", "")) in wrong_flags.get(target_key, set()):
                    continue
                matched[source] = m

    if not matched:
        raise HTTPException(status_code=404, detail="Race not found")

    first = list(matched.values())[0]
    canonical_key = market_race_key(first)

    # Mark (but don't strip) entries the admin flagged as wrong. The listing
    # and divergence calculator filter flagged markets out, but the detail
    # page keeps them visible so the admin can see what they flagged and
    # undo it. Non-admin users will see them struck-through in the UI.
    flagged_here = wrong_flags.get(canonical_key, set()) | wrong_flags.get(race_key, set())
    flags_for_race = state.db.get_flags_for_race(canonical_key)
    flag_notes_by_pair = {
        (f["source"], f["source_id"]): f.get("note")
        for f in flags_for_race
    }

    verification = (
        state.db.get_race_verification(canonical_key)
        or state.db.get_race_verification(race_key)
    )
    return {
        "race_key": canonical_key,
        "title": first.get("title"),
        "event_title": first.get("event_title"),
        "race_type": first.get("race_type"),
        "state": first.get("state"),
        "district": target_district,
        "verified": bool(verification),
        "verified_by": (verification or {}).get("reviewer_email"),
        "verified_at": (verification or {}).get("verified_at"),
        "flags": flags_for_race,
        "by_source": {
            s: {
                "outcomes": m.get("outcomes", []),
                "title": m.get("title"),
                "volume": m.get("volume", 0),
                "liquidity": m.get("liquidity", 0),
                "slug": m.get("slug", ""),
                "source_id": m.get("source_id", ""),
                "flagged": (s, m.get("source_id", "")) in flagged_here,
                "flag_note": flag_notes_by_pair.get((s, m.get("source_id", ""))),
            }
            for s, m in matched.items()
        },
    }


@app.get("/data/race/{race_key}/candidates")
async def data_race_candidates(race_key: str, refresh: bool = False):
    """Extract candidates from a race's market outcomes and enrich each with Wikipedia.

    Returns a list of {name, party?, probability?, description?, extract?, url?, thumbnail?}.
    Skips yes/no and party-only markets. Caches results in the jurisdiction profile so
    we don't hammer Wikipedia on every page view.
    """
    # Reuse the race detail logic to find the canonical race
    detail = await data_race_detail(race_key)
    sources = detail.get("by_source", {})

    # Aggregate candidates across sources, preferring multi-outcome markets
    # (these have actual candidate names, not yes/no)
    candidate_outcomes: dict[str, dict] = {}
    for src, data in sources.items():
        outcomes = data.get("outcomes", [])
        names = [(o.get("name") or "").strip() for o in outcomes]
        # Skip yes/no markets and party-only markets
        if not names or len(names) < 2:
            continue
        if all(n.lower() in ("yes", "no") for n in names if n):
            continue
        if all(n.lower() in ("republican", "democratic", "democrat", "republican party",
                              "democratic party", "other", "independent") for n in names if n):
            continue
        # This source has real candidates
        for o in outcomes:
            name = (o.get("name") or "").strip()
            if not name or name.lower() in ("yes", "no"):
                continue
            # Skip generic party labels
            if name.lower() in ("republican", "democratic", "democrat", "republican party",
                                 "democratic party", "other", "independent"):
                continue
            existing = candidate_outcomes.get(name)
            if existing is None or (o.get("probability") or 0) > (existing.get("probability") or 0):
                candidate_outcomes[name] = {
                    "name": name,
                    "probability": o.get("probability"),
                    "source": src,
                }

    if not candidate_outcomes:
        return {"race_key": race_key, "candidates": [], "note": "No candidate-level outcomes found"}

    # Sort by probability desc, take top 10
    sorted_candidates = sorted(
        candidate_outcomes.values(),
        key=lambda c: c.get("probability") or 0,
        reverse=True,
    )[:10]

    # Check cache: if we have cached candidates for this race_key, return them unless refresh
    if not refresh:
        cached = state.db.get_jurisdiction_profile("race_candidates", race_key)
        if cached and cached.get("candidates_data"):
            try:
                cached_list = json.loads(cached["candidates_data"])
                # Update probabilities with current values
                cached_by_name = {c.get("name"): c for c in cached_list}
                merged = []
                for sc in sorted_candidates:
                    bio = cached_by_name.get(sc["name"], {})
                    merged.append({**bio, **sc})
                return {"race_key": race_key, "candidates": merged}
            except (json.JSONDecodeError, TypeError):
                pass

    # Build a context hint + state name to disambiguate common names
    # e.g. "Mike Collins" → score Wikipedia hits by whether they mention "Georgia"
    rt = detail.get("race_type", "")
    st = detail.get("state", "")
    state_name_hint: Optional[str] = None
    context_parts = []
    if st and st != "US":
        from data_sources.fips import state_to_name
        from data_sources.countries import country_name
        state_name_hint = state_to_name(st) or country_name(st) or st
        context_parts.append(state_name_hint)
    if rt and rt != "world":
        context_parts.append(rt)
    context_hint = " ".join(context_parts) if context_parts else None

    # Fresh enrichment: fetch Wikipedia bio + FEC financials for each candidate
    from data_sources.wikipedia import fetch_person_bio
    from data_sources.fec import fetch_race_financials, match_fec_to_candidate

    # Fetch FEC data for the race (House/Senate only — FEC doesn't cover governors)
    dist_code = detail.get("district")
    fec_candidates = await fetch_race_financials(
        state.http_session,
        st,
        rt,
        district=dist_code,
        cycle=2026,
    )

    enriched = []
    for c in sorted_candidates:
        bio = await fetch_person_bio(
            state.http_session,
            c["name"],
            context=context_hint,
            state_name=state_name_hint,
        )
        entry = {**c}
        if bio:
            entry.update({
                "description": bio.get("description"),
                "extract": bio.get("extract"),
                "url": bio.get("url"),
                "thumbnail": bio.get("thumbnail"),
            })
        # Match to FEC financials
        fec_match = match_fec_to_candidate(fec_candidates, c["name"])
        if fec_match:
            entry["fec"] = {
                "receipts": fec_match["receipts"],
                "disbursements": fec_match["disbursements"],
                "cash_on_hand": fec_match["cash_on_hand"],
                "party": fec_match["party"],
                "candidate_id": fec_match["candidate_id"],
            }
        enriched.append(entry)

    # Cache to DB
    try:
        state.db.upsert_jurisdiction_profile(
            jurisdiction_type="race_candidates",
            jurisdiction_code=race_key,
            name=race_key,
            profile_data={"updated_at": _iso_now()},
            candidates_data=enriched,
            auto_generated=True,
        )
    except Exception as e:
        logger.warning(f"Failed to cache candidates for {race_key}: {e}")

    return {"race_key": race_key, "candidates": enriched}


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


@app.get("/data/history/{race_key}")
async def data_history(race_key: str, days: int = 30):
    """Historical price/divergence data for a race."""
    history = state.db.get_divergence_history(race_key=race_key, days=days)
    # Reformat for chart consumption
    chart_data = []
    for h in history:
        point = {"date": h.get("snapshot_time", "")[:10]}
        if h.get("polymarket_prob") is not None:
            point["polymarket"] = h["polymarket_prob"]
        if h.get("kalshi_prob") is not None:
            point["kalshi"] = h["kalshi_prob"]
        if h.get("predictit_prob") is not None:
            point["predictit"] = h["predictit_prob"]
        if h.get("polling_avg") is not None:
            point["polling"] = h["polling_avg"]
        chart_data.append(point)
    return {"race_key": race_key, "days": days, "history": chart_data}


@app.get("/data/divergence")
async def data_divergence():
    """Current divergence data across all sources."""
    divergences = state.db.get_divergence_history(days=1)
    # Deduplicate by race_key, keeping latest
    latest = {}
    for d in divergences:
        rk = d.get("race_key")
        if rk and (rk not in latest or d.get("snapshot_time", "") > latest[rk].get("snapshot_time", "")):
            latest[rk] = d
    return {"divergences": list(latest.values())}


@app.get("/data/divergence/history/{race_key}")
async def data_divergence_history(race_key: str, days: int = 30):
    """Divergence over time for a specific race."""
    history = state.db.get_divergence_history(race_key=race_key, days=days)
    return {"race_key": race_key, "days": days, "history": history}


@app.get("/data/sources")
async def data_sources():
    """Data sources and their status."""
    all_markets = state.db.get_all_markets(active_only=True)
    sources = defaultdict(lambda: {"market_count": 0, "status": "ok", "last_updated": None})
    for m in all_markets:
        s = m.get("source", "unknown")
        sources[s]["market_count"] += 1
        lu = m.get("last_updated")
        if lu and (sources[s]["last_updated"] is None or lu > sources[s]["last_updated"]):
            sources[s]["last_updated"] = lu
    return {"sources": dict(sources)}


@app.get("/data/polling/recent")
async def data_polling_recent(limit: int = 50):
    """Most recent polls across all races."""
    polls = state.db.get_recent_polls(limit=limit)
    return {"polls": [
        {
            "pollster": p.get("pollster"),
            "candidate": p.get("candidate"),
            "party": p.get("party"),
            "percentage": p.get("percentage"),
            "sample_size": p.get("sample_size"),
            "state": p.get("state"),
            "poll_type": p.get("poll_type"),
            "start_date": p.get("start_date"),
            "end_date": p.get("end_date"),
        }
        for p in polls
    ]}


@app.get("/data/polling/{race_key}")
async def data_polling(race_key: str):
    """Raw polling data for a specific race.

    The race_key format is ``{poll_type}_{state}`` (e.g. ``senate_PA``).
    If the key contains only one segment it is treated as a national-level
    poll_type with no state filter.
    """
    parts = race_key.split("_", 1)
    poll_type = parts[0] if parts else race_key
    state_abbr = parts[1] if len(parts) > 1 else None

    polls = state.db.get_polls(state=state_abbr, poll_type=poll_type)

    results = []
    for p in polls:
        results.append({
            "pollster": p.get("pollster"),
            "candidate": p.get("candidate"),
            "party": p.get("party"),
            "percentage": p.get("percentage"),
            "sample_size": p.get("sample_size"),
            "start_date": p.get("start_date"),
            "end_date": p.get("end_date"),
        })

    return {"race_key": race_key, "poll_type": poll_type, "state": state_abbr, "polls": results}


@app.get("/data/historical")
async def data_historical(
    year: Optional[int] = None,
    race_type: Optional[str] = None,
    state: Optional[str] = None,
):
    """Historical US election results (presidential, senate, governor).

    Returns past winners with vote totals, percentages, and margins so users
    can compare current prediction markets against prior outcomes.
    Filter by year, race_type (president/senate/governor), and/or state.
    """
    from historical_results import get_results, HISTORICAL_RESULTS
    results = get_results(year=year, race_type=race_type, state=state)
    # Available filter options
    all_years = sorted({r["year"] for r in HISTORICAL_RESULTS}, reverse=True)
    all_types = sorted({r["race_type"] for r in HISTORICAL_RESULTS})
    all_states = sorted({r["state"] for r in HISTORICAL_RESULTS})
    return {
        "results": results,
        "filters": {
            "years": all_years,
            "race_types": all_types,
            "states": all_states,
        },
    }


# In-process cache for backtest weights so /data/forecasts doesn't recompute
# the per-source Brier numbers on every request. Refreshed every 10 minutes;
# the coverage / Brier values change at most as often as new resolved races
# show up in the historical dataset.
_BACKTEST_CACHE: dict = {"ts": 0.0, "data": None}
_BACKTEST_CACHE_TTL = 600.0


async def _backtest_summary_cached() -> dict:
    now = time.time()
    if _BACKTEST_CACHE["data"] and (now - _BACKTEST_CACHE["ts"]) < _BACKTEST_CACHE_TTL:
        return _BACKTEST_CACHE["data"]
    # Look back further than the public default so weights stabilize from a
    # larger sample once 2026 races start resolving.
    data = await data_backtest(since_days=180)
    _BACKTEST_CACHE["data"] = data
    _BACKTEST_CACHE["ts"] = now
    return data


def _polymarket_markets_for_race(race_key: str) -> list[dict]:
    """Return the polymarket-sourced midterm_markets rows that belong to ``race_key``.

    Used by the smart-money endpoint to translate a race key into the slugs
    we'll join against the upstream flow data.
    """
    all_markets = state.db.get_all_markets(active_only=True)
    out = []
    for m in all_markets:
        if m.get("source") != "polymarket":
            continue
        if market_race_key(m) == race_key:
            out.append(m)
    return out


@app.get("/data/smart-money/{race_key}")
async def data_smart_money(race_key: str):
    """Aggregated top-trader positioning for this race.

    Joins the global smart-money flow list (from ``top-traders-dashboard``)
    against the polymarket markets stored for this race. Returns the schema
    documented in ``smart_money.race_smart_money``.
    """
    flows_payload = await fetch_smart_money_flows(state.http_session)
    if not flows_payload.get("available"):
        return {
            "race_key": race_key,
            "available": False,
            "reason": flows_payload.get("reason", "no_data"),
            "total_smart_usd": 0.0,
            "smart_wallet_count": 0,
            "direction": None,
            "lean_strength": 0.0,
            "flows": [],
        }
    markets = _polymarket_markets_for_race(race_key)
    return race_smart_money(
        race_key=race_key,
        race_polymarket_markets=markets,
        flows=flows_payload.get("flows", []),
    )


@app.get("/data/news/recent")
async def data_news_recent(limit: int = 30):
    """Latest political news ingested from RSS feeds.

    Untagged items are included so users can see the full stream — the
    ``race_key`` field tells the frontend whether a piece is wired to a race.
    """
    items = state.db.get_recent_news(limit=min(max(1, limit), 200))
    return {"items": items, "total": len(items)}


@app.get("/data/news/race/{race_key}")
async def data_news_for_race(race_key: str, limit: int = 20):
    """Recent news tagged to this race, with measured reactions joined in.

    Each item carries any ``reactions`` we've recorded so far — list of
    ``{source, market_id, baseline_price, reaction_price, delta_pp, lag_seconds}``
    for the markets in this race.
    """
    items = state.db.get_recent_news(race_key=race_key, limit=min(max(1, limit), 200))
    reactions = state.db.get_news_reactions(race_key=race_key, limit=500)
    by_news: dict[int, list[dict]] = defaultdict(list)
    for r in reactions:
        by_news[r["news_id"]].append({
            "source": r["source"],
            "market_id": r.get("market_id"),
            "baseline_price": r.get("baseline_price"),
            "reaction_price": r.get("reaction_price"),
            "delta_pp": r.get("delta_pp"),
            "lag_seconds": r.get("lag_seconds"),
        })
    for item in items:
        item["reactions"] = by_news.get(item["id"], [])
    return {"race_key": race_key, "items": items, "total": len(items)}


@app.get("/data/news/lag-curve")
async def data_news_lag_curve(min_delta_pp: float = 1.0, limit: int = 1000):
    """Per-source median time-to-reprice after a news event.

    Aggregates every recorded reaction whose price move exceeded
    ``min_delta_pp`` percentage points. Yields a per-source dictionary of
    {median_lag_s, median_delta_pp, n}. The smaller the median lag, the
    faster that source's market reacts to news — a genuine market-quality
    proxy that no paid election tracker exposes.
    """
    reactions = state.db.get_news_reactions(limit=limit)
    curve = lag_curve(reactions, min_delta_pp=min_delta_pp)
    return curve


@app.get("/data/forecast/{race_key}")
async def data_forecast(race_key: str):
    """narve.ai house forecast for a single race.

    The forecast is a Brier-weighted ensemble of every source that has a
    probability for this race. Sources without enough resolved-race coverage
    fall back to static priors. Returns the schema documented in
    ``forecast.forecast_for_race``.
    """
    from forecast import forecast_for_race

    snap = state.db.get_latest_divergence(race_key)
    if not snap:
        return {
            "race_key": race_key,
            "forecast_d": None,
            "confidence": 0.0,
            "sources_used": [],
            "source_probs": {},
            "weights": {},
            "spread": None,
            "n_sources": 0,
            "method": "default_weights",
            "available": False,
        }

    bt = await _backtest_summary_cached()
    source_probs: dict[str, float] = {}
    for src, col in (
        ("polymarket", "polymarket_prob"),
        ("kalshi", "kalshi_prob"),
        ("predictit", "predictit_prob"),
        ("polling", "polling_avg"),
    ):
        v = snap.get(col)
        if v is not None:
            try:
                source_probs[src] = float(v)
            except (TypeError, ValueError):
                pass
    details = snap.get("divergence_details") or {}
    if isinstance(details, dict):
        for src in ("manifold", "metaculus"):
            v = details.get(src)
            if v is not None:
                try:
                    source_probs[src] = float(v)
                except (TypeError, ValueError):
                    pass

    f = forecast_for_race(
        race_key=race_key,
        source_probs=source_probs,
        brier=bt.get("brier"),
        coverage=bt.get("coverage"),
    )
    f["race_type"] = snap.get("race_type")
    f["state"] = snap.get("state")
    f["snapshot_time"] = snap.get("snapshot_time")
    f["available"] = f["forecast_d"] is not None

    # Attach the smart-money signal so a single fetch powers the whole badge.
    # If top-traders is offline we still return the forecast; the smart_money
    # block will just be empty.
    try:
        sm_flows = await fetch_smart_money_flows(state.http_session)
        if sm_flows.get("available"):
            sm = race_smart_money(
                race_key=race_key,
                race_polymarket_markets=_polymarket_markets_for_race(race_key),
                flows=sm_flows.get("flows", []),
            )
            # Drop the verbose flow list from the inlined version; the
            # dedicated /data/smart-money/{race_key} endpoint exposes detail.
            f["smart_money"] = {
                "available": sm["available"],
                "total_smart_usd": sm["total_smart_usd"],
                "smart_wallet_count": sm["smart_wallet_count"],
                "avg_quality": sm["avg_quality"],
                "direction": sm["direction"],
                "lean_strength": sm["lean_strength"],
                "by_party": sm.get("by_party", {}),
            }
        else:
            f["smart_money"] = {"available": False}
    except Exception as e:
        logger.warning(f"smart-money attach failed for {race_key}: {e}")
        f["smart_money"] = {"available": False}

    return f


@app.get("/data/forecasts")
async def data_forecasts(
    race_type: Optional[str] = None,
    min_confidence: float = 0.0,
    limit: int = 200,
):
    """narve.ai house forecasts across every active race.

    Sorted by absolute confidence × volume of source agreement so the most
    "interesting" races bubble up. Filters by ``race_type`` if provided.
    Smart-money signals are inlined when available — the frontend uses them
    to highlight races where the proven-quality wallets disagree with the
    market consensus (a "smart-money divergence").
    """
    from forecast import forecast_many

    snaps = state.db.get_latest_divergence_per_race()
    bt = await _backtest_summary_cached()
    rows = forecast_many(
        snaps,
        brier=bt.get("brier"),
        coverage=bt.get("coverage"),
    )

    if race_type:
        rows = [r for r in rows if (r.get("race_type") or "").lower() == race_type.lower()]
    rows = [r for r in rows if r.get("forecast_d") is not None and (r.get("confidence") or 0) >= min_confidence]

    # Attach smart money to every row in one pass. We fetch the global flow
    # list once and then index it; per-race work is just a slug join.
    try:
        sm_flows = await fetch_smart_money_flows(state.http_session)
    except Exception as e:
        logger.warning(f"smart-money batch fetch failed: {e}")
        sm_flows = {"flows": [], "available": False}

    sm_available = sm_flows.get("available", False)
    flow_list = sm_flows.get("flows", []) if sm_available else []
    if flow_list:
        # Pre-compute the polymarket markets index once.
        all_pm = [m for m in state.db.get_all_markets(active_only=True) if m.get("source") == "polymarket"]
        markets_by_race: dict[str, list[dict]] = defaultdict(list)
        for m in all_pm:
            markets_by_race[market_race_key(m)].append(m)
        for r in rows:
            rk = r.get("race_key")
            if not rk:
                continue
            sm = race_smart_money(
                race_key=rk,
                race_polymarket_markets=markets_by_race.get(rk, []),
                flows=flow_list,
            )
            r["smart_money"] = {
                "available": sm["available"],
                "total_smart_usd": sm["total_smart_usd"],
                "smart_wallet_count": sm["smart_wallet_count"],
                "direction": sm["direction"],
                "lean_strength": sm["lean_strength"],
            }
    else:
        for r in rows:
            r["smart_money"] = {"available": False}

    # Sort: high confidence first, then most polarized (closest to 0 or 1).
    def sort_key(r):
        f = r.get("forecast_d") or 0.5
        return (-(r.get("confidence") or 0), -abs(f - 0.5))

    rows.sort(key=sort_key)
    return {
        "forecasts": rows[:limit],
        "total": len(rows),
        "method": rows[0].get("method") if rows else "default_weights",
        "smart_money_available": sm_available,
    }


def _polling_avg_d_by_race() -> dict[str, float]:
    """Compute a Dem-share polling average per race.

    Dem share = D% / (D% + R%) from recent polls in the race's state. We
    intentionally do this in Python (rather than SQL) because party labels
    in the poll table are free-form strings ("Democrat", "DEM", "D", etc.).
    """
    polls = state.db.get_polls()
    by_race: dict[str, dict[str, float]] = defaultdict(lambda: {"d_sum": 0.0, "r_sum": 0.0, "n": 0})
    for p in polls:
        st = (p.get("state") or "").strip().upper()
        ptype = (p.get("poll_type") or "").strip().lower()
        pct = p.get("percentage")
        party_raw = (p.get("party") or "").strip().lower()
        if not st or not ptype or pct is None:
            continue
        if ptype == "generic_ballot":
            # Generic ballot rolls into the national race buckets; skip per-race
            # joining for now since we don't have a national-only race key.
            continue
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            continue
        if party_raw.startswith("d"):
            key = f"{ptype}_{st}"
            by_race[key]["d_sum"] += pct
            by_race[key]["n"] += 1
        elif party_raw.startswith("r"):
            key = f"{ptype}_{st}"
            by_race[key]["r_sum"] += pct
            by_race[key]["n"] += 1

    out: dict[str, float] = {}
    for race_key, agg in by_race.items():
        total = agg["d_sum"] + agg["r_sum"]
        if total <= 0:
            continue
        out[race_key] = round(agg["d_sum"] / total, 4)
    return out


@app.get("/data/forecast/conditional")
async def data_forecast_conditional(given: str):
    """Re-score every race conditional on one race resolving for D or R.

    Query parameter ``given`` is ``"<race_key>=<D|R>"``, e.g.
    ``given=senate_PA=D``. Internally we run the common-factor swing model
    in ``conditional.py`` so the response includes a per-race ``delta_pp``
    showing how the conditional shifts each race vs the unconditional
    forecast.

    Powers the interactive map: hover a state and the page recolours every
    other state based on the implied conditional forecast.
    """
    if "=" not in given:
        raise HTTPException(400, "given must be of form '<race_key>=<D|R>'")
    race_key, outcome = given.split("=", 1)
    outcome = outcome.strip().upper()
    if outcome not in ("D", "R"):
        raise HTTPException(400, "outcome must be D or R")

    base = await data_forecasts(min_confidence=0.0, limit=10_000)
    forecasts = base.get("forecasts", []) or []
    result = compute_conditional(
        forecasts=forecasts,
        conditioned_race_key=race_key.strip(),
        conditioned_outcome=outcome,
    )
    return result


@app.get("/data/calibration")
async def data_calibration(since_days: int = 365):
    """Per-confidence-bucket calibration table + over-time Brier trend.

    Builds samples from every divergence snapshot in the window. For each
    snapshot we compute the ensemble forecast on-the-fly (matching what the
    user would have seen at that point) and join against
    ``HISTORICAL_RESULTS`` to get the realized outcome. Snapshots without
    a matching resolved race are skipped.

    Returns ``{"table": {...}, "over_time": {...}, "in_sample": bool}``.
    The in_sample flag tells the frontend whether the calibration was
    measured against the same races the Brier weights were trained on —
    important caveat to surface.
    """
    from forecast import forecast_for_race
    from historical_results import HISTORICAL_RESULTS

    # Build {(race_type, state) → outcome_d} from the curated dataset.
    resolved: dict[tuple[str, str], int] = {}
    for r in HISTORICAL_RESULTS:
        key = (r["race_type"], r["state"].upper())
        # Only keep the most recent year per (chamber, state).
        if key not in resolved or r["year"] > resolved.get(key + ("year",), 0):
            resolved[key] = 1 if (r.get("party") or "").upper() == "D" else 0

    snapshots = state.db.get_divergence_history(since_days=since_days)
    bt = await _backtest_summary_cached()
    samples: list[dict] = []
    for snap in snapshots:
        rt = (snap.get("race_type") or "").lower()
        st = (snap.get("state") or "").upper()
        key = (rt, st)
        if key not in resolved:
            continue
        details = snap.get("divergence_details") or {}
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except json.JSONDecodeError:
                details = {}
        probs: dict[str, float] = {}
        for src, col in (
            ("polymarket", "polymarket_prob"),
            ("kalshi", "kalshi_prob"),
            ("predictit", "predictit_prob"),
            ("polling", "polling_avg"),
        ):
            v = snap.get(col)
            if v is not None:
                try:
                    probs[src] = float(v)
                except (TypeError, ValueError):
                    pass
        if isinstance(details, dict):
            for src in ("manifold", "metaculus"):
                v = details.get(src)
                if v is not None:
                    try:
                        probs[src] = float(v)
                    except (TypeError, ValueError):
                        pass
        if not probs:
            continue
        f = forecast_for_race(
            race_key=snap.get("race_key", ""),
            source_probs=probs,
            brier=bt.get("brier"),
            coverage=bt.get("coverage"),
        )
        if f["forecast_d"] is None:
            continue
        samples.append({
            "forecast_d": f["forecast_d"],
            "outcome_d": resolved[key],
            "snapshot_time": snap.get("snapshot_time"),
            "race_key": snap.get("race_key"),
        })

    return {
        "table": calibration_table(samples),
        "over_time": calibration_over_time(samples, n_windows=6),
        "n_samples": len(samples),
        # Calibration is in-sample today (same races feed Brier weights and
        # this measurement). Flip to False once we have forward-looking
        # forecast snapshots beyond resolved 2026 races.
        "in_sample": True,
    }


@app.get("/data/forecast/wave")
async def data_forecast_wave(swing_pp: float = 0.0):
    """Scenario: apply a fixed national swing (in pp) to every race.

    ``swing_pp`` positive = wave toward D, negative = wave toward R. Powers
    the interactive wave slider on the map page: drag from R+10 → D+10 and
    watch the chamber control bars + map flip in real time.
    """
    base = await data_forecasts(min_confidence=0.0, limit=10_000)
    forecasts = base.get("forecasts", []) or []
    return apply_wave_swing(forecasts, swing_pp=swing_pp)


@app.get("/data/forecast/joint-summary")
async def data_forecast_joint_summary():
    """Monte-Carlo expected D / R seats per chamber under the swing model.

    Smoother than counting ``forecast_d >= 0.5`` because the swing model
    captures the across-race correlation that makes wave outcomes more
    plausible than independent coin flips would suggest.
    """
    base = await data_forecasts(min_confidence=0.0, limit=10_000)
    forecasts = base.get("forecasts", []) or []
    out = {}
    for chamber in ("senate", "house", "governor"):
        out[chamber] = joint_distribution_summary(forecasts, chamber=chamber)
    return out


@app.get("/data/election-night")
async def data_election_night():
    """Race-night master view: synthetic narve.ai calls + chamber totals + polling gap.

    Combines:
      * ``/data/forecasts`` output (forecast_d, confidence, smart_money) for
        every active race
      * A polling Dem-share map per ``{race_type}_{state}`` key
      * The ``election_night`` module's call-state machine + aggregation

    This single endpoint powers the dedicated election-night page. The SSE
    push-bus already broadcasts ``data_updated`` events after each refresh
    cycle, so the frontend re-fetches this whole snapshot sub-minute.
    """
    payload = await data_forecasts(min_confidence=0.0, limit=10_000)
    forecasts = payload.get("forecasts", []) or []
    polling = _polling_avg_d_by_race()

    en = assemble_election_night(forecasts=forecasts, polling_by_race=polling)
    en["smart_money_available"] = payload.get("smart_money_available", False)
    en["method"] = payload.get("method")
    return en


@app.get("/data/backtest")
async def data_backtest(since_days: int = 30):
    """Per-source backtest against the curated historical-results dataset.

    For every divergence snapshot in the last ``since_days`` days whose race
    matches a row in ``HISTORICAL_RESULTS``, compute the Brier score
    ``(p - outcome)^2`` for each source's probability vs. the actual winning
    party. The lower the Brier score the better calibrated that source was on
    that race; aggregating across races yields a per-source quality metric.

    Returns:
      * ``coverage`` — how many divergence snapshots, races, and resolved
        races each source contributed to.
      * ``brier`` — mean Brier score per source (only over resolved races).
      * ``samples`` — sampled (race_key, source, prob, winner) rows so the
        frontend can plot a calibration chart.

    Caveats:
      * The historical dataset is hand-curated and small; full coverage will
        only arrive once 2026 races resolve.
      * "Probability" here is treated as P(Democratic win). Markets that
        present a different outcome require the canonical-question matcher to
        normalise — for now we only score control / senate / governor races
        whose outcomes can be unambiguously reduced to D vs R.
    """
    from historical_results import HISTORICAL_RESULTS, winning_party

    snapshots = state.db.get_divergence_history(since_days=since_days)

    # Build a (race_type, state) → year map of resolved races so we can match
    # snapshots to outcomes. We only count the *most recent* historical row for
    # a given (race_type, state) — older races don't have current divergence.
    resolved_by_race: dict[tuple[str, str], dict] = {}
    for r in HISTORICAL_RESULTS:
        key = (r["race_type"], r["state"].upper())
        if key not in resolved_by_race or r["year"] > resolved_by_race[key]["year"]:
            resolved_by_race[key] = r

    SOURCE_COLS = {
        "polymarket": "polymarket_prob",
        "kalshi": "kalshi_prob",
        "predictit": "predictit_prob",
        "polling": "polling_avg",
    }

    coverage = {src: {"snapshots": 0, "races": set(), "resolved_races": set()}
                for src in list(SOURCE_COLS) + ["manifold", "metaculus"]}
    brier_totals = {src: {"sum": 0.0, "n": 0} for src in coverage}
    samples: list[dict] = []

    for snap in snapshots:
        rt = (snap.get("race_type") or "").lower()
        st = (snap.get("state") or "").upper()
        race_key = snap.get("race_key") or ""
        # Probabilities for the major sources are denormalised columns;
        # secondary sources live in ``divergence_details``.
        details = snap.get("divergence_details") or {}
        all_probs = {}
        for src, col in SOURCE_COLS.items():
            v = snap.get(col)
            if v is not None:
                all_probs[src] = float(v)
        for src in ("manifold", "metaculus"):
            v = details.get(src) if isinstance(details, dict) else None
            if v is not None:
                try:
                    all_probs[src] = float(v)
                except (ValueError, TypeError):
                    pass

        for src, p in all_probs.items():
            coverage[src]["snapshots"] += 1
            coverage[src]["races"].add(race_key)

        # Score against historical winner if we have one
        hist = resolved_by_race.get((rt, st))
        if not hist:
            continue
        winning = (hist.get("party") or "").upper()
        if winning not in {"D", "R"}:
            continue
        # By convention probabilities here represent P(D wins). For races where
        # the snapshot was about R-controlled outcomes we'd need the matcher;
        # assume canonical D-orientation for the basic backtest.
        outcome = 1.0 if winning == "D" else 0.0

        for src, p in all_probs.items():
            coverage[src]["resolved_races"].add(race_key)
            brier_totals[src]["sum"] += (p - outcome) ** 2
            brier_totals[src]["n"] += 1
            samples.append({
                "race_key": race_key,
                "source": src,
                "prob_d": p,
                "winner": winning,
                "year": hist.get("year"),
                "snapshot_time": snap.get("snapshot_time"),
            })

    return {
        "since_days": since_days,
        "snapshots_total": len(snapshots),
        "coverage": {
            src: {
                "snapshots": v["snapshots"],
                "races": len(v["races"]),
                "resolved_races": len(v["resolved_races"]),
            }
            for src, v in coverage.items()
        },
        "brier": {
            src: round(v["sum"] / v["n"], 4) if v["n"] else None
            for src, v in brier_totals.items()
        },
        "samples": samples[:500],
    }


@app.get("/data/race-context/{race_key}")
async def data_race_context(race_key: str):
    """Policy context, referendums, and key issues for a race.

    race_key format: "{race_type}_{state}" e.g. "senate_GA", "governor_FL"
    """
    from race_context import get_context, get_all_contexts
    ctx = get_context(*race_key.split("_", 1)) if "_" in race_key else None
    if ctx:
        return {"race_key": race_key, **ctx}
    return {"race_key": race_key, "found": False}


@app.get("/data/race-contexts")
async def data_race_contexts():
    """All race contexts."""
    from race_context import get_all_contexts
    return {"contexts": get_all_contexts()}


@app.get("/data/district-profile/{state_abbr}")
async def data_district_profile(state_abbr: str):
    """Comprehensive state/district profile: demographics, economy, infrastructure,
    political history, geography, education, and key facts.

    Used by RaceDetail and Historical pages to provide background context for a race's location.
    """
    profile = state.db.get_district_profile(state_abbr)
    if profile:
        return {
            "state": profile["state"],
            "name": profile["name"],
            "auto_generated": bool(profile.get("auto_generated")),
            "updated_at": profile.get("updated_at"),
            **profile["profile_data"],
        }

    # Fallback: check static data directly
    from district_profiles import get_profile, generate_basic_profile
    static = get_profile(state_abbr)
    if static:
        # Store it for next time
        state.db.upsert_district_profile(
            state=state_abbr,
            name=static.get("name", state_abbr),
            profile_data=static,
            auto_generated=False,
        )
        return {"state": state_abbr.upper(), "name": static.get("name", state_abbr), **static}

    return {"state": state_abbr.upper(), "found": False}


@app.get("/data/district-profiles")
async def data_district_profiles():
    """List all available district/state profiles (metadata only, not full data)."""
    profiles = state.db.get_all_district_profiles()
    return {
        "profiles": [
            {
                "state": p["state"],
                "name": p["name"],
                "auto_generated": bool(p.get("auto_generated")),
                "updated_at": p.get("updated_at"),
                "has_population": bool(p.get("profile_data", {}).get("population", {}).get("total")),
            }
            for p in profiles
        ]
    }


# ============================================================================
# Unified jurisdiction profiles (US states, US House districts, countries)
# ============================================================================

@app.get("/data/jurisdiction-profile")
async def data_jurisdiction_profile(
    jurisdiction_type: str,
    jurisdiction_code: str,
    refresh: bool = False,
):
    """Fetch a unified jurisdiction profile.

    Parameters:
        jurisdiction_type: 'us_state' | 'us_district' | 'country'
        jurisdiction_code: e.g. 'GA', 'TX-28', 'HU'
        refresh: if True, fetch fresh data from external sources and re-cache

    Returns the merged profile (live data from Census/BEA/BLS/WorldBank/Wikipedia
    layered on top of any curated static data).
    """
    jt = jurisdiction_type.lower()
    jc = jurisdiction_code.upper()

    if jt not in ("us_state", "us_district", "country"):
        raise HTTPException(status_code=400, detail="invalid jurisdiction_type")

    # Check cache first (unless refresh requested)
    if not refresh:
        cached = state.db.get_jurisdiction_profile(jt, jc)
        if cached:
            return {
                "jurisdiction_type": jt,
                "jurisdiction_code": jc,
                "name": cached["name"],
                "auto_generated": bool(cached.get("auto_generated")),
                "updated_at": cached.get("updated_at"),
                "candidates": cached.get("candidates_data") or [],
                **(cached.get("profile_data") or {}),
            }

    # Build fresh
    from data_sources import (
        enrich_state_profile,
        enrich_house_district_profile,
        enrich_country_profile,
    )

    base: dict = {}
    if jt == "us_state":
        # Layer on top of any curated static data
        from district_profiles import get_profile as get_static
        base = get_static(jc) or {}
        profile = await enrich_state_profile(state.http_session, jc, base=base)
        name = profile.get("name") or base.get("name") or jc
    elif jt == "us_district":
        # jc is "TX-28" or "WY-AL"
        try:
            st_abbr, district = jc.split("-", 1)
        except ValueError:
            raise HTTPException(status_code=400, detail="us_district code must be 'STATE-NN'")
        profile = await enrich_house_district_profile(state.http_session, st_abbr, district)
        name = profile.get("name") or jc
    else:  # country
        profile = await enrich_country_profile(state.http_session, jc)
        name = profile.get("name") or jc

    # Cache it
    state.db.upsert_jurisdiction_profile(
        jurisdiction_type=jt,
        jurisdiction_code=jc,
        name=name,
        profile_data=profile,
        auto_generated=False,
    )
    return {
        "jurisdiction_type": jt,
        "jurisdiction_code": jc,
        "name": name,
        "auto_generated": False,
        "updated_at": profile.get("_enriched_at"),
        "candidates": [],
        **profile,
    }


@app.get("/data/jurisdiction-profiles")
async def data_jurisdiction_profiles(jurisdiction_type: Optional[str] = None):
    """List all jurisdiction profiles (metadata only)."""
    rows = state.db.get_all_jurisdiction_profiles(jurisdiction_type)
    return {"profiles": rows}


@app.get("/data/world-elections")
async def data_world_elections(
    country: Optional[str] = None,
    source: Optional[str] = None,
    search: Optional[str] = None,
    min_volume: Optional[float] = None,
):
    """World leader election markets from prediction platforms."""
    raw_markets = state.db.get_markets(
        race_type="world", state=country, source=source,
        search=search, min_volume=min_volume,
    )
    # Shallow-copy each row to avoid mutating cached objects shared with
    # other handlers.
    markets = []
    for raw in raw_markets:
        m = dict(raw)
        m["race_key"] = f"world_{m.get('state') or 'INTL'}"
        m["market_id"] = f"{m.get('source', 'unknown')}_{m.get('source_id', '')}"
        markets.append(m)
    return {"markets": markets}


# ===================================================================
# PREMIUM ENDPOINTS
# ===================================================================

@app.get("/premium/alerts")
async def premium_alerts(request: Request):
    user = await require_tier(request, "premium")
    alerts = state.db.get_alerts(user["id"])
    return {"alerts": alerts}


@app.post("/premium/alerts")
async def premium_create_alert(body: AlertBody, request: Request):
    user = await require_tier(request, "premium")
    state.db.upsert_alert(user["id"], body.race_key, body.threshold or 5.0)
    return {"ok": True, "race_key": body.race_key}


@app.get("/premium/watchlist")
async def premium_watchlist(request: Request):
    user = await require_tier(request, "premium")
    watchlist = state.db.get_watchlist(user["id"])
    return {"watchlist": watchlist}


@app.post("/premium/watchlist/{race_key}")
async def premium_watchlist_add(race_key: str, request: Request):
    user = await require_tier(request, "premium")
    state.db.add_to_watchlist(user["id"], race_key)
    return {"ok": True, "race_key": race_key}


@app.delete("/premium/watchlist/{race_key}")
async def premium_watchlist_remove(race_key: str, request: Request):
    user = await require_tier(request, "premium")
    state.db.remove_from_watchlist(user["id"], race_key)
    return {"ok": True, "race_key": race_key}


@app.get("/premium/detailed-comparison/{race_key}")
async def premium_detailed_comparison(race_key: str, request: Request):
    """Deep comparison with orderbook data."""
    user = await require_tier(request, "premium")
    all_markets = state.db.get_all_markets(active_only=True)
    matched = {}
    for m in all_markets:
        partial = f"{m.get('race_type', 'other')}_{m.get('state', 'US')}"
        if partial == race_key or m.get("source_id") == race_key:
            matched[m.get("source", "unknown")] = m

    if not matched:
        raise HTTPException(status_code=404, detail="Race not found")

    orderbooks = {}
    for source, market in matched.items():
        outcomes = market.get("outcomes", [])
        if source == "polymarket" and outcomes:
            token_id = outcomes[0].get("token_id")
            if token_id:
                try:
                    orderbooks["polymarket"] = await state.polymarket.fetch_orderbook(token_id)
                except Exception as e:
                    logger.warning(f"Polymarket orderbook error: {e}")
        elif source == "kalshi":
            ticker = market.get("source_id")
            if ticker:
                try:
                    orderbooks["kalshi"] = await state.kalshi.fetch_orderbook(ticker)
                except Exception as e:
                    logger.warning(f"Kalshi orderbook error: {e}")

    first = list(matched.values())[0]
    return {
        "race_key": race_key,
        "race": {
            "title": first.get("title"),
            "state": first.get("state"),
            "race_type": first.get("race_type"),
            "by_source": {s: {"outcomes": m.get("outcomes", [])} for s, m in matched.items()},
        },
        "orderbooks": orderbooks,
    }


@app.get("/premium/campaign-finance/{state_abbr}")
async def premium_campaign_finance(state_abbr: str, request: Request):
    """FEC fundraising data placeholder."""
    user = await require_tier(request, "premium")
    return {"state": state_abbr, "finance": [], "note": "FEC integration coming soon"}


# ===================================================================
# ADMIN ENDPOINTS
# ===================================================================

@app.get("/admin/stats")
async def admin_stats(request: Request):
    await require_tier(request, "admin")
    stats = state.db.get_admin_stats()
    return stats


@app.get("/admin/users")
async def admin_users(request: Request, limit: int = 100, offset: int = 0):
    await require_tier(request, "admin")
    users = state.db.get_all_users(limit=limit, offset=offset)
    return {"users": users}


@app.get("/admin/audit-log")
async def admin_audit_log(request: Request, limit: int = 100):
    await require_tier(request, "admin")
    entries = state.db.get_audit_log(limit=limit)
    return {"logs": entries}


@app.post("/admin/race/{race_key}/flag")
async def admin_flag_market(race_key: str, body: FlagMarketBody, request: Request):
    """Flag a market as NOT belonging to *race_key*.

    Idempotent: flagging the same (source, source_id, race_key) twice just
    updates the reviewer/note. The matching layer excludes flagged markets
    from the race bucket on the next request and on the next divergence pass.
    """
    user = await require_tier(request, "admin")
    state.db.flag_market_as_wrong(
        source=body.source,
        source_id=body.source_id,
        race_key=race_key,
        reviewer_id=user.get("id"),
        reviewer_email=user.get("email"),
        note=body.note,
    )
    return {"ok": True, "flagged": {"source": body.source, "source_id": body.source_id, "race_key": race_key}}


@app.delete("/admin/race/{race_key}/flag/{source}/{source_id}")
async def admin_unflag_market(race_key: str, source: str, source_id: str, request: Request):
    await require_tier(request, "admin")
    removed = state.db.unflag_market(source=source, source_id=source_id, race_key=race_key)
    if not removed:
        raise HTTPException(status_code=404, detail="Flag not found")
    return {"ok": True}


@app.post("/admin/race/{race_key}/verify")
async def admin_verify_race(race_key: str, body: VerifyRaceBody, request: Request):
    """Mark the current source pairing for *race_key* as human-verified."""
    user = await require_tier(request, "admin")
    state.db.verify_race(
        race_key=race_key,
        reviewer_id=user.get("id"),
        reviewer_email=user.get("email"),
        note=body.note,
    )
    return {"ok": True, "race_key": race_key}


@app.delete("/admin/race/{race_key}/verify")
async def admin_unverify_race(race_key: str, request: Request):
    await require_tier(request, "admin")
    removed = state.db.unverify_race(race_key)
    if not removed:
        raise HTTPException(status_code=404, detail="Verification not found")
    return {"ok": True}


@app.get("/admin/data-status")
async def admin_data_status(request: Request):
    await require_tier(request, "admin")
    all_markets = state.db.get_all_markets(active_only=True)
    sources = defaultdict(lambda: {"market_count": 0, "status": "ok", "last_updated": None})
    for m in all_markets:
        s = m.get("source", "unknown")
        sources[s]["market_count"] += 1
        lu = m.get("last_updated")
        if lu and (sources[s]["last_updated"] is None or lu > sources[s]["last_updated"]):
            sources[s]["last_updated"] = lu
    return {"sources": dict(sources)}


# ===================================================================
# FX rates proxy (frankfurter.dev) — cached, USD base
# ===================================================================

_fx_cache: dict = {"data": None, "fetched_at": 0.0}
_FX_TTL = 3600  # 1 hour
_FX_FALLBACK = {
    "base": "USD",
    "date": "fallback",
    "rates": {
        "USD": 1.0, "EUR": 0.92, "GBP": 0.79, "JPY": 150.0, "AUD": 1.52,
        "CAD": 1.36, "CHF": 0.88, "CNY": 7.20, "HKD": 7.83, "NZD": 1.65,
        "SEK": 10.5, "KRW": 1340.0, "SGD": 1.34, "NOK": 10.6, "MXN": 17.0,
        "INR": 83.0, "ZAR": 18.5, "TRY": 32.0, "BRL": 5.0, "DKK": 6.85,
        "PLN": 3.95, "THB": 35.0, "IDR": 15700.0, "HUF": 360.0, "CZK": 23.0,
        "ILS": 3.7, "PHP": 56.0, "MYR": 4.7, "RON": 4.6, "ISK": 137.0,
    },
}


@app.get("/api/fx-rates")
async def get_fx_rates():
    """Return USD-base FX rates, cached for 1h. Source: frankfurter.dev."""
    now = time.time()
    cached = _fx_cache["data"]
    if cached and (now - _fx_cache["fetched_at"]) < _FX_TTL:
        return cached
    try:
        async with state.http_session.get(
            "https://api.frankfurter.dev/v1/latest?base=USD",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as r:
            if r.status == 200:
                data = await r.json()
                # Ensure USD = 1 is included for round-tripping
                data.setdefault("rates", {})
                data["rates"]["USD"] = 1.0
                _fx_cache["data"] = data
                _fx_cache["fetched_at"] = now
                return data
    except Exception as e:
        logger.warning(f"FX rate fetch failed: {e}")
    # Serve stale cache if we have one, else fallback
    if cached:
        return cached
    return _FX_FALLBACK


# ===================================================================
# Public v1 API — registered after all /data/* handlers so v1 can delegate
# to them. The v1 endpoints have permissive CORS via public_cors_middleware.
# ===================================================================

api_v1.register(app, get_state=lambda: state)


# ===================================================================
# Static file serving for React SPA (production)
# ===================================================================

_frontend_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"

# ===================================================================
# CROSS-DASHBOARD SHARE ENDPOINT (localhost-only, for sibling services)
# ===================================================================

@app.get("/api/share/top-races")
async def share_top_races(request: Request):
    """Lightweight summary for cross-dashboard integration (world-state, etc.)."""
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="localhost only")

    all_markets = state.db.get_all_markets(active_only=True)

    # --- Control probabilities ---
    senate_control = {}
    house_control = {}
    for m in all_markets:
        if m.get("race_type") != "control":
            continue
        title = (m.get("title") or "").lower()
        outcomes = m.get("outcomes", [])
        target = senate_control if "senate" in title else (house_control if "house" in title else None)
        if target is None or not outcomes:
            continue
        src = m.get("source", "unknown")
        dem = rep = 0
        for o in outcomes:
            n = (o.get("name") or "").lower()
            p = o.get("probability") or 0
            if "democrat" in n or n in ("dem", "dems") or "yes" in n:
                dem = p
            elif "republican" in n or n in ("rep", "reps", "gop") or n == "no":
                rep = p
        if not rep and dem:
            rep = 1 - dem
        target[src] = {"dem": round(dem, 3), "rep": round(rep, 3)}

    # --- Top races by volume ---
    race_markets = [m for m in all_markets if m.get("race_type") in ("senate", "house", "governor", "presidential")]
    race_markets.sort(key=lambda m: m.get("volume") or 0, reverse=True)

    races = []
    seen_keys = set()
    for m in race_markets:
        rk = f"{m.get('race_type','')}-{m.get('state','')}-{m.get('district','')}"
        if rk in seen_keys:
            continue
        seen_keys.add(rk)
        outcomes = m.get("outcomes", [])
        top = max(outcomes, key=lambda o: o.get("probability", 0)) if outcomes else {}
        races.append({
            "race_type": m.get("race_type"),
            "state": m.get("state"),
            "district": m.get("district"),
            "title": m.get("title", "")[:120],
            "source": m.get("source"),
            "top_candidate": top.get("name", ""),
            "top_prob": round(top.get("probability", 0), 3),
            "volume": m.get("volume", 0),
        })
        if len(races) >= 20:
            break

    return {
        "senate_control": senate_control,
        "house_control": house_control,
        "top_races": races,
        "total_markets": len(all_markets),
    }


if _frontend_dist.is_dir():
    # Serve static assets (js, css, images) under /assets
    app.mount(
        "/assets",
        StaticFiles(directory=str(_frontend_dist / "assets")),
        name="static-assets",
    )

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Serve the React SPA index.html for all non-API routes."""
        # If the request matches a real file, serve it
        file_path = (_frontend_dist / full_path).resolve()
        if full_path and file_path.is_file() and str(file_path).startswith(str(_frontend_dist.resolve())):
            return FileResponse(str(file_path))
        # Otherwise serve index.html for client-side routing
        index = _frontend_dist / "index.html"
        if index.is_file():
            return FileResponse(str(index))
        raise HTTPException(status_code=404, detail="Not found")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=PORT,
        reload=bool(os.getenv("DEV")),
        log_level="info",
    )

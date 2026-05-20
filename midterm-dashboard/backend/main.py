from __future__ import annotations
import asyncio
import hmac
import json
import logging
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import Database
from aggregators import (
    PolymarketAggregator,
    KalshiAggregator,
    PredictItAggregator,
    PollingAggregator,
)
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
# Logging + Sentry
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("midterm-dashboard")

# Sentry is opt-in via SENTRY_DSN. Captures unhandled exceptions in routes and
# background tasks. Traces sample rate is small in production by default.
_SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
if _SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.05")),
            profiles_sample_rate=float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE", "0.0")),
            environment=os.getenv("ENVIRONMENT", "production"),
            release=os.getenv("RELEASE", None),
            send_default_pii=False,
        )
        logger.info("Sentry initialized")
    except Exception as e:
        logger.warning(f"Sentry init failed: {e}")

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
    alert_type: Optional[str] = "divergence"  # "divergence" or "move"


class FlagMarketBody(BaseModel):
    source: str
    source_id: str
    note: Optional[str] = None


class VerifyRaceBody(BaseModel):
    note: Optional[str] = None


class PushSubscriptionBody(BaseModel):
    endpoint: str
    keys: dict


class CommentBody(BaseModel):
    body: str


class PaperPositionBody(BaseModel):
    race_key: str
    source: str
    outcome: str
    side: str  # "yes" or "no"
    entry_price: float
    size_usd: float
    note: Optional[str] = None


class ResolutionBody(BaseModel):
    race_key: str
    race_type: str
    state: str
    winner: str
    winning_party: Optional[str] = None
    notes: Optional[str] = None


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

    def __init__(self):
        self.background_tasks: list[asyncio.Task] = []
        # In-memory rate-limit store: {ip: [timestamps]}
        self.rate_limit_store: dict[str, list[float]] = defaultdict(list)
        # Per-source fetch health, surfaced via /data/sources and /admin/data-status.
        # Each value is {last_success, last_error, last_error_message, market_count}.
        self.source_health: dict[str, dict] = {}


state = AppState()


def _record_source_success(source: str, count: int) -> None:
    """Record a successful fetch for *source*."""
    now = datetime.now(timezone.utc).isoformat()
    s = state.source_health.setdefault(source, {})
    s["last_success"] = now
    s["last_fetch_count"] = count
    s["last_error"] = s.get("last_error")  # preserve prior error timestamp


def _record_source_error(source: str, message: str) -> None:
    """Record a failed fetch for *source*."""
    now = datetime.now(timezone.utc).isoformat()
    s = state.source_health.setdefault(source, {})
    s["last_error"] = now
    s["last_error_message"] = message[:200]

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

def _check_rate_limit(ip: str, tier: str) -> bool:
    """Return True if the request is allowed, False if rate-limited."""
    limit = RATE_LIMITS.get(tier, 60)
    if limit == 0:
        return True  # unlimited

    now = time.time()
    window = 60.0

    # Prune entries older than the window
    cutoff = now - window
    # Note: relies on state.rate_limit_store being a defaultdict(list)
    state.rate_limit_store[ip] = [t for t in state.rate_limit_store[ip] if t > cutoff]

    # Clean up empty keys to prevent unbounded memory growth from blocked IPs
    if not state.rate_limit_store[ip]:
        del state.rate_limit_store[ip]
        # After cleanup the list is empty, so this request is within limits —
        # fall through to the append below.
    elif len(state.rate_limit_store[ip]) >= limit:
        return False

    # Only record the timestamp if the request is allowed
    state.rate_limit_store.setdefault(ip, []).append(now)
    return True


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
                return_exceptions=True,
            )
            poly_data, kalshi_data, pi_data, poll_data, poly_world = results

            # Kalshi world uses cached data from the midterm fetch above
            try:
                kalshi_world = await with_timeout(
                    state.kalshi.fetch_world_election_markets(), "Kalshi-World", seconds=30
                )
            except Exception as e:
                logger.error(f"Kalshi-World fetch error: {e}")
                kalshi_world = e

            # Store midterm markets
            for source_key, label, data in [
                ("polymarket", "Polymarket", poly_data),
                ("kalshi", "Kalshi", kalshi_data),
                ("predictit", "PredictIt", pi_data),
            ]:
                if isinstance(data, list):
                    state.db.upsert_markets_batch(data)
                    logger.info(f"Stored {len(data)} {label} markets")
                    _record_source_success(source_key, len(data))
                else:
                    logger.error(f"{label} fetch error: {data}")
                    _record_source_error(source_key, str(data))

            # Store polls
            if isinstance(poll_data, dict):
                all_polls = []
                for poll_type, polls in poll_data.items():
                    all_polls.extend(polls)
                if all_polls:
                    state.db.store_polls_batch(all_polls)
                logger.info(f"Stored {len(all_polls)} polls")
                _record_source_success("538", len(all_polls))
            else:
                logger.error(f"Polling fetch error: {poll_data}")
                _record_source_error("538", str(poll_data))

            # Store world election markets
            for source_key, label, data in [
                ("polymarket_world", "Polymarket world", poly_world),
                ("kalshi_world", "Kalshi world", kalshi_world),
            ]:
                if isinstance(data, list):
                    state.db.upsert_markets_batch(data)
                    logger.info(f"Stored {len(data)} {label} markets")
                    _record_source_success(source_key, len(data))
                else:
                    logger.error(f"{label} fetch error: {data}")
                    _record_source_error(source_key, str(data))

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

            snapshots: list[dict] = []
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

                snapshots.append({
                    "race_key": race_key,
                    "state": state_abbr,
                    "race_type": race_type,
                    "data": {
                        "polymarket": source_probs.get("polymarket"),
                        "kalshi": source_probs.get("kalshi"),
                        "predictit": source_probs.get("predictit"),
                        "polling": source_probs.get("polling"),
                        "max_divergence": round(max_div, 4),
                        "details": source_probs,
                    },
                })

            state.db.record_divergence_batch(snapshots)
            logger.info(f"Divergence calculated for {len(snapshots)} races")
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
# Alert delivery worker
# ---------------------------------------------------------------------------

ALERT_CHECK_INTERVAL = 120  # 2 minutes between alert checks


def _latest_top_prob_by_race(markets: list[dict]) -> dict[str, dict]:
    """Return race_key -> {source: top_probability}."""
    from collections import defaultdict as _dd
    out: dict[str, dict[str, float]] = _dd(dict)
    for m in markets:
        rk = market_race_key(m)
        if rk.startswith("unmatched_"):
            continue
        outcomes = m.get("outcomes") or []
        if not outcomes:
            continue
        top = outcomes[0].get("probability")
        if top is None:
            continue
        out[rk][m.get("source", "unknown")] = top
    return dict(out)


async def alert_delivery_loop():
    """Fires user alerts when their watched race crosses configured thresholds.

    For each enabled alert:
      - alert_type=divergence: fire when max-min spread across sources >= threshold
      - alert_type=move: fire when the top-source probability moves >= threshold
        in percentage points since the last fire (or since first observed)

    The watermark in ``midterm_alert_dedup`` prevents re-firing on every cycle
    until the value reverts past the threshold or moves another *threshold* pp.

    Delivery channels: web push (if user has a subscription) and email (if
    SMTP is configured + we have the user's email from the profiles table).
    Both channels are best-effort; failure to deliver is logged but doesn't
    block subsequent alerts.
    """
    from notifications import send_email, send_web_push
    while True:
        try:
            alerts = state.db.get_all_enabled_alerts()
            if alerts:
                all_markets = state.db.get_all_markets(active_only=True)
                top_by_race = _latest_top_prob_by_race(all_markets)
                fired = 0
                for a in alerts:
                    rk = a.get("race_key")
                    uid = a.get("user_id")
                    if not rk or not uid:
                        continue
                    source_probs = top_by_race.get(rk, {})
                    if not source_probs:
                        continue
                    alert_type = a.get("alert_type") or "divergence"
                    threshold_pp = float(a.get("threshold") or 5.0)

                    if alert_type == "divergence":
                        if len(source_probs) < 2:
                            continue
                        probs = list(source_probs.values())
                        spread_pp = (max(probs) - min(probs)) * 100
                        if spread_pp < threshold_pp:
                            continue
                        wm = state.db.get_alert_watermark(uid, rk, alert_type) or {}
                        prev = wm.get("last_probability")
                        # Re-fire only if spread has grown beyond the previous
                        # fired spread by another threshold-worth of points
                        # (or this is the first fire).
                        if prev is not None and abs(spread_pp - prev * 100) < threshold_pp:
                            continue
                        msg = f"Spread {spread_pp:.1f}pp across sources for {rk}"
                        state.db.record_alert_fired(uid, rk, alert_type, spread_pp / 100)
                        state.db.log_alert(uid, rk, alert_type, msg)
                    else:  # "move"
                        # Use the polymarket probability if present, else first
                        primary = source_probs.get("polymarket")
                        if primary is None:
                            primary = next(iter(source_probs.values()))
                        wm = state.db.get_alert_watermark(uid, rk, alert_type) or {}
                        prev = wm.get("last_probability")
                        if prev is None:
                            state.db.record_alert_fired(uid, rk, alert_type, primary)
                            continue  # baseline, no fire
                        move_pp = abs(primary - prev) * 100
                        if move_pp < threshold_pp:
                            continue
                        direction = "up" if primary > prev else "down"
                        msg = f"{rk} moved {move_pp:.1f}pp {direction} (now {primary*100:.1f}%)"
                        state.db.record_alert_fired(uid, rk, alert_type, primary)
                        state.db.log_alert(uid, rk, alert_type, msg)

                    # Deliver
                    fired += 1
                    user_email = a.get("email")
                    if user_email:
                        await send_email(
                            user_email,
                            subject=f"MidtermEdge alert: {rk}",
                            html=f"<p>{msg}</p><p><a href=\"https://midterm.narve.ai/race/{rk}\">View race</a></p>",
                            text=msg,
                        )
                    for sub in state.db.get_push_subscriptions(uid):
                        await send_web_push(
                            {"endpoint": sub["endpoint"], "keys": sub["keys"]},
                            {"title": "MidtermEdge", "body": msg, "race_key": rk, "url": f"/race/{rk}"},
                        )
                if fired:
                    logger.info(f"Alert worker fired {fired} alerts this cycle")
        except Exception as e:
            logger.error(f"Alert worker error: {e}", exc_info=True)
        await asyncio.sleep(ALERT_CHECK_INTERVAL)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing application")
    state.db = Database()
    state.db.connect()

    state.http_session = aiohttp.ClientSession()

    state.polymarket = PolymarketAggregator(session=state.http_session)
    state.kalshi = KalshiAggregator(session=state.http_session)
    state.predictit = PredictItAggregator(session=state.http_session)
    state.polling = PollingAggregator(session=state.http_session)

    # Seed district profiles from static data on startup
    _seed_district_profiles()

    # Seed the accuracy backtest dataset (idempotent upserts).
    try:
        from accuracy import seed_from_curated_dataset
        n_res, n_pred = seed_from_curated_dataset(state.db)
        logger.info(f"Accuracy backtest seeded: {n_res} resolutions, {n_pred} predictions")
    except Exception as e:
        logger.warning(f"Accuracy backtest seeding failed: {e}")

    # Start background tasks
    state.background_tasks = [
        asyncio.create_task(data_refresh_loop(), name="data_refresh"),
        asyncio.create_task(divergence_calculator(), name="divergence"),
        asyncio.create_task(district_profile_updater(), name="district_profiles"),
        asyncio.create_task(jurisdiction_profile_updater(), name="jurisdiction_profiles"),
        asyncio.create_task(alert_delivery_loop(), name="alert_worker"),
    ]

    # Optional Polymarket WS consumer — gated on POLYMARKET_WS_ENABLED so the
    # default deployment continues to use 5-min polling.
    from aggregators.polymarket_ws import PolymarketWebSocket, ws_enabled
    if ws_enabled():
        state.polymarket_ws = PolymarketWebSocket(state.db, session=state.http_session)
        state.background_tasks.append(
            asyncio.create_task(state.polymarket_ws.run(), name="polymarket_ws")
        )
        logger.info("Polymarket WS consumer started")

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


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'"
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
# PUBLIC DATA ENDPOINTS
# ===================================================================

@app.get("/data/overview")
async def data_overview():
    """Senate/House control probabilities from all sources."""
    all_markets = state.db.get_all_markets(active_only=True)

    # Build control overview from "control" type markets
    senate_sources = {}
    house_sources = {}
    source_summary = defaultdict(lambda: {"market_count": 0, "status": "ok"})

    for m in all_markets:
        source = m.get("source", "unknown")
        source_summary[source]["market_count"] += 1

        if m.get("race_type") != "control":
            continue
        title = (m.get("title") or "").lower()
        outcomes = m.get("outcomes", [])

        target = None
        if "senate" in title:
            target = senate_sources
        elif "house" in title:
            target = house_sources

        if target is not None and outcomes:
            dem_prob = 0
            rep_prob = 0
            for o in outcomes:
                name = (o.get("name") or "").lower()
                prob = o.get("probability") or 0
                if "democrat" in name or name in ("dem", "dems") or "yes" in name:
                    dem_prob = prob
                elif "republican" in name or name in ("rep", "reps", "gop") or name == "no":
                    rep_prob = prob
            if not rep_prob and dem_prob:
                rep_prob = 1 - dem_prob
            target[source] = {"democrat": dem_prob, "republican": rep_prob}

    return {
        "senate_control": {"sources": senate_sources},
        "house_control": {"sources": house_sources},
        "source_summary": dict(source_summary),
    }


import re as _re


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
    if _re.search(r"hold exactly \d+ senate", text) or "senate seats" in text:
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
    """Data sources and their status, including last successful fetch timestamps."""
    all_markets = state.db.get_all_markets(active_only=True)
    sources: dict[str, dict] = defaultdict(
        lambda: {"market_count": 0, "status": "ok", "last_updated": None}
    )
    for m in all_markets:
        s = m.get("source", "unknown")
        sources[s]["market_count"] += 1
        lu = m.get("last_updated")
        if lu and (sources[s]["last_updated"] is None or lu > sources[s]["last_updated"]):
            sources[s]["last_updated"] = lu

    # Merge in per-fetch health tracked in memory by the refresh loop.
    for src, health in state.source_health.items():
        bucket = sources.setdefault(src, {"market_count": 0, "status": "ok", "last_updated": None})
        bucket["last_success"] = health.get("last_success")
        bucket["last_error"] = health.get("last_error")
        bucket["last_error_message"] = health.get("last_error_message")
        # Status = "stale" if the most recent attempt was an error newer than success
        ls = health.get("last_success") or ""
        le = health.get("last_error") or ""
        if le and le > ls:
            bucket["status"] = "error"
        elif ls:
            bucket["status"] = "ok"

    return {"sources": dict(sources)}


def _comparison_rows(race_type: Optional[str] = None, state_abbr: Optional[str] = None) -> list[dict]:
    """Build cross-source comparison rows for the /data/comparison and CSV
    export endpoints. Each row has the top-outcome probability per source
    plus a ``spread`` (max-min in percentage points)."""
    markets = state.db.get_markets(race_type=race_type, state=state_abbr)
    valid = {"senate", "house", "governor", "control", "presidential", "world"}
    markets = [m for m in markets if m.get("race_type") in valid]

    wrong_flags = state.db.get_all_wrong_flags()

    by_race: dict[str, dict] = {}
    for raw in markets:
        m = dict(raw)
        rk = market_race_key(m)
        if rk.startswith("unmatched_"):
            continue
        pair = (m.get("source", ""), m.get("source_id", ""))
        if pair in wrong_flags.get(rk, set()):
            continue
        outcomes = m.get("outcomes", []) or []
        if not outcomes:
            continue
        top = outcomes[0].get("probability")
        if top is None:
            continue
        bucket = by_race.setdefault(rk, {
            "race_key": rk,
            "title": m.get("event_title") or m.get("title"),
            "race_type": m.get("race_type"),
            "state": m.get("state"),
        })
        # Keep the highest-volume source representation per race
        src = m.get("source", "unknown")
        existing = bucket.get(f"_{src}_volume", -1)
        if (m.get("volume") or 0) > existing:
            bucket[src] = top
            bucket[f"_{src}_volume"] = m.get("volume") or 0

    rows = []
    for rk, b in by_race.items():
        sources_present = [s for s in ("polymarket", "kalshi", "predictit", "polling") if b.get(s) is not None]
        if len(sources_present) < 2:
            continue
        probs = [b[s] for s in sources_present]
        spread_pp = (max(probs) - min(probs)) * 100
        # Strip the per-source volume helpers
        for k in list(b.keys()):
            if k.startswith("_"):
                del b[k]
        b["spread"] = round(spread_pp, 2)
        b["source_count"] = len(sources_present)
        rows.append(b)

    rows.sort(key=lambda r: r["spread"], reverse=True)
    return rows


@app.get("/data/comparison")
async def data_comparison(
    race_type: Optional[str] = None,
    state_abbr: Optional[str] = None,
):
    """Side-by-side cross-source probability table for every multi-source race.

    Powers the /compare frontend page. Each row carries the top-outcome
    probability for each source plus the spread (max-min, percentage points).
    """
    return {"rows": _comparison_rows(race_type, state_abbr)}


@app.get("/data/export/races.csv")
async def data_export_races_csv(
    race_type: Optional[str] = None,
    state_abbr: Optional[str] = None,
    search: Optional[str] = None,
):
    """CSV download of every active race with per-source probabilities."""
    import csv
    import io

    markets = state.db.get_markets(
        race_type=race_type, state=state_abbr, search=search,
    )
    wrong_flags = state.db.get_all_wrong_flags()

    by_race: dict[str, dict] = {}
    for raw in markets:
        m = dict(raw)
        rk = market_race_key(m)
        if rk.startswith("unmatched_"):
            continue
        pair = (m.get("source", ""), m.get("source_id", ""))
        if pair in wrong_flags.get(rk, set()):
            continue
        outcomes = m.get("outcomes", []) or []
        top = outcomes[0].get("probability") if outcomes else None
        bucket = by_race.setdefault(rk, {
            "race_key": rk,
            "title": m.get("event_title") or m.get("title") or "",
            "race_type": m.get("race_type") or "",
            "state": m.get("state") or "",
            "polymarket": "",
            "kalshi": "",
            "predictit": "",
            "polling": "",
            "volume": 0.0,
        })
        bucket["volume"] += float(m.get("volume") or 0)
        src = m.get("source") or ""
        if src in bucket and top is not None:
            bucket[src] = round(top, 4)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["race_key", "title", "race_type", "state",
                     "polymarket", "kalshi", "predictit", "polling", "volume"])
    for rk in sorted(by_race.keys()):
        r = by_race[rk]
        writer.writerow([
            r["race_key"], r["title"], r["race_type"], r["state"],
            r["polymarket"], r["kalshi"], r["predictit"], r["polling"],
            f"{r['volume']:.0f}",
        ])

    from fastapi.responses import Response
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="midterm-races.csv"'},
    )


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
    from historical_results import get_results, HISTORICAL_RESULTS, LAST_VERIFIED
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
        "last_verified": LAST_VERIFIED,
    }


@app.get("/data/race-context/{race_key}")
async def data_race_context(race_key: str):
    """Policy context, referendums, and key issues for a race.

    race_key format: "{race_type}_{state}" e.g. "senate_GA", "governor_FL"
    """
    from race_context import get_context, LAST_VERIFIED
    ctx = get_context(*race_key.split("_", 1)) if "_" in race_key else None
    if ctx:
        return {"race_key": race_key, "last_verified": LAST_VERIFIED, **ctx}
    return {"race_key": race_key, "found": False, "last_verified": LAST_VERIFIED}


@app.get("/data/race-contexts")
async def data_race_contexts():
    """All race contexts."""
    from race_context import get_all_contexts, LAST_VERIFIED
    return {"contexts": get_all_contexts(), "last_verified": LAST_VERIFIED}


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
    sources: dict[str, dict] = defaultdict(
        lambda: {"market_count": 0, "status": "ok", "last_updated": None}
    )
    for m in all_markets:
        s = m.get("source", "unknown")
        sources[s]["market_count"] += 1
        lu = m.get("last_updated")
        if lu and (sources[s]["last_updated"] is None or lu > sources[s]["last_updated"]):
            sources[s]["last_updated"] = lu
    for src, health in state.source_health.items():
        bucket = sources.setdefault(src, {"market_count": 0, "status": "ok", "last_updated": None})
        bucket["last_success"] = health.get("last_success")
        bucket["last_error"] = health.get("last_error")
        bucket["last_error_message"] = health.get("last_error_message")
        ls = health.get("last_success") or ""
        le = health.get("last_error") or ""
        if le and le > ls:
            bucket["status"] = "error"
        elif ls:
            bucket["status"] = "ok"
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
# Push subscriptions + notification config
# ===================================================================

@app.get("/data/push/public-key")
async def data_push_public_key():
    """The VAPID public key the frontend needs to subscribe to push."""
    from notifications import vapid_public_key, channels_available
    return {
        "public_key": vapid_public_key(),
        "channels": channels_available(),
    }


@app.post("/premium/push/subscribe")
async def premium_push_subscribe(body: PushSubscriptionBody, request: Request):
    user = await require_tier(request, "premium")
    state.db.add_push_subscription(user["id"], body.endpoint, body.keys)
    return {"ok": True}


@app.post("/premium/push/unsubscribe")
async def premium_push_unsubscribe(body: PushSubscriptionBody, request: Request):
    user = await require_tier(request, "premium")
    removed = state.db.remove_push_subscription(user["id"], body.endpoint)
    return {"ok": removed}


@app.get("/premium/alerts/history")
async def premium_alert_history(request: Request, limit: int = 50):
    user = await require_tier(request, "premium")
    return {"history": state.db.get_alert_history(user["id"], limit=limit)}


# ===================================================================
# Race comments
# ===================================================================

@app.get("/data/race/{race_key}/comments")
async def data_race_comments(race_key: str):
    """Public list of comments on a race."""
    comments = state.db.get_comments(race_key)
    return {"comments": [
        {
            "id": c["id"],
            "user_email": (c.get("user_email") or "").split("@")[0],
            "user_tier": c.get("user_tier"),
            "body": c["body"],
            "created_at": c["created_at"],
        }
        for c in comments
    ]}


@app.post("/premium/race/{race_key}/comments")
async def premium_create_comment(race_key: str, body: CommentBody, request: Request):
    user = await require_tier(request, "premium")
    text = (body.body or "").strip()
    if not text or len(text) > 2000:
        raise HTTPException(status_code=400, detail="Comment must be 1-2000 chars")
    cid = state.db.add_comment(race_key, user["id"], user["email"], user.get("tier", "free"), text)
    return {"ok": True, "id": cid}


@app.delete("/premium/comments/{comment_id}")
async def premium_delete_comment(comment_id: int, request: Request):
    user = await require_tier(request, "premium")
    # Admins can delete any comment; others only their own.
    target = None if user.get("tier") == "admin" else user["id"]
    deleted = state.db.delete_comment(comment_id, user_id=target)
    if not deleted:
        raise HTTPException(status_code=404, detail="Comment not found")
    return {"ok": True}


# ===================================================================
# Paper portfolio
# ===================================================================

@app.get("/premium/portfolio")
async def premium_portfolio(request: Request, open_only: bool = False):
    user = await require_tier(request, "premium")
    positions = state.db.get_paper_positions(user["id"], open_only=open_only)
    return {"positions": positions}


@app.post("/premium/portfolio")
async def premium_portfolio_open(body: PaperPositionBody, request: Request):
    user = await require_tier(request, "premium")
    if body.side not in ("yes", "no"):
        raise HTTPException(status_code=400, detail="side must be 'yes' or 'no'")
    if body.entry_price <= 0 or body.entry_price >= 1:
        raise HTTPException(status_code=400, detail="entry_price must be between 0 and 1")
    if body.size_usd <= 0 or body.size_usd > 1_000_000:
        raise HTTPException(status_code=400, detail="size_usd must be between 0 and 1_000_000")
    pid = state.db.open_paper_position(
        user_id=user["id"], race_key=body.race_key, source=body.source,
        outcome=body.outcome, side=body.side, entry_price=body.entry_price,
        size_usd=body.size_usd, note=body.note,
    )
    return {"ok": True, "id": pid}


@app.delete("/premium/portfolio/{position_id}")
async def premium_portfolio_close(position_id: int, request: Request, exit_price: float = 0.0):
    user = await require_tier(request, "premium")
    if exit_price < 0 or exit_price > 1:
        raise HTTPException(status_code=400, detail="exit_price must be between 0 and 1")
    closed = state.db.close_paper_position(position_id, user["id"], exit_price)
    if not closed:
        raise HTTPException(status_code=404, detail="Position not found or already closed")
    return {"ok": True}


# ===================================================================
# Accuracy backtest (resolved races vs. source predictions)
# ===================================================================

@app.post("/admin/resolve")
async def admin_resolve_race(body: ResolutionBody, request: Request):
    await require_tier(request, "admin")
    state.db.upsert_resolution(
        race_key=body.race_key, race_type=body.race_type, state=body.state,
        winner=body.winner, winning_party=body.winning_party, notes=body.notes,
    )
    return {"ok": True}


@app.get("/data/accuracy")
async def data_accuracy():
    """Calibration / accuracy stats per source over resolved races.

    Pulls from the curated historical predictions table (see
    ``accuracy_backfill.py``) and computes Brier scores, hit rates, and
    calibration on the toss-up bucket for every source × race-type slice.
    """
    from accuracy import compute_summary
    predictions = state.db.get_historical_predictions()
    return {
        "summary": compute_summary(predictions),
        "methodology": {
            "metrics": {
                "hit_rate": "Fraction of races where the source assigned ≥50% to the eventual winner.",
                "brier": "Mean (1 - prob_of_winner)^2. 0 = perfect, 0.25 = coinflip, 1 = maximally wrong.",
                "calibration_50": "Among predictions in the 40–60% bucket, fraction where the predicted winner actually won. A well-calibrated forecaster lands near 0.5 here.",
            },
            "data_provenance": "Closing prices curated from Polymarket on-chain history, PredictIt historical pages, Kalshi (2024 only), and 538 polling averages. See backend/accuracy_backfill.py for the full dataset.",
        },
    }


@app.get("/data/accuracy/badge/{source}")
async def data_accuracy_badge(source: str, race_type: Optional[str] = None):
    """Single-source slice for inline badges on race cards.

    Returned shape is intentionally narrow so a badge component doesn't
    have to crawl the full ``/data/accuracy`` payload on every render.
    """
    from accuracy import compute_source_stats
    predictions = state.db.get_historical_predictions(source=source)
    stats = compute_source_stats(predictions, race_type=race_type)
    bucket = stats.get(source)
    if not bucket:
        return {
            "source": source, "race_type": race_type, "available": False,
            "n": 0, "hit_rate": None, "brier": None,
        }
    return {
        "source": source,
        "race_type": race_type,
        "available": True,
        "n": bucket["n"],
        "hit_rate": bucket["hit_rate"],
        "brier": bucket["brier"],
        "calibration_50": bucket["calibration_50"],
        "n_toss_ups": bucket["n_toss_ups"],
    }


# ===================================================================
# Movement explanations (scaffolded — uses recent volume + history)
# ===================================================================

@app.get("/data/race/{race_key}/movements")
async def data_race_movements(race_key: str, hours: int = 24):
    """Recent price movements + grounded LLM explanations.

    Pulls news from NewsAPI (primary, needs NEWS_API_KEY) and GDELT 2.0
    (free fallback), then asks Claude to identify which articles plausibly
    caused the movement. Hard rules in the system prompt + response
    validation prevent fabricated citations. Results cached 1 hour per
    (race_key, hour_bucket) to bound LLM cost.

    Disabled paths:
      - No ANTHROPIC_API_KEY: returns movements + empty explanation
        with a clear "configured: false" flag.
      - Movement < 1.5pp on every source: short-circuits without an LLM
        call (returns "insufficient_movement").
    """
    if hours <= 0 or hours > 168:
        hours = 24
    from movement_analysis import analyze_movement

    # Pull race title + race_context for query targeting
    race_title = race_key
    race_context_data = None
    try:
        from race_context import get_context
        if "_" in race_key:
            rt, st = race_key.split("_", 1)
            race_context_data = get_context(rt, st.split("-", 1)[0])
    except Exception:
        race_context_data = None

    # Parse race_type/state from the race_key
    race_type = ""
    state_abbr = ""
    if "_" in race_key:
        parts = race_key.split("_", 1)
        race_type = parts[0]
        state_abbr = parts[1].split("-", 1)[0] if len(parts) > 1 else ""

    result = await analyze_movement(
        db=state.db,
        session=state.http_session,
        race_key=race_key,
        race_title=race_title,
        race_type=race_type,
        state=state_abbr,
        hours=hours,
        race_context=race_context_data,
    )
    return {"race_key": race_key, **result}


@app.get("/data/movements/config")
async def data_movements_config():
    """Which providers are wired up for movement explanations."""
    from llm import llm_configured
    from news import channels_available as news_channels
    return {
        "llm": {"configured": llm_configured()},
        "news": news_channels(),
    }


# ===================================================================
# SEO + Share: sitemap, robots, OG images, embed
# ===================================================================

@app.get("/sitemap.xml")
async def sitemap_xml():
    """Sitemap listing every active race so search engines crawl detail pages."""
    from fastapi.responses import Response
    base = os.getenv("PUBLIC_BASE_URL", "https://midterm.narve.ai").rstrip("/")
    static_routes = ["/", "/races", "/compare", "/divergence", "/world", "/historical"]
    all_markets = state.db.get_all_markets(active_only=True)
    race_keys: set[str] = set()
    for m in all_markets:
        rk = market_race_key(m)
        if not rk.startswith("unmatched_"):
            race_keys.add(rk)

    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for r in static_routes:
        lines.append(f"<url><loc>{base}{r}</loc><changefreq>hourly</changefreq></url>")
    for rk in sorted(race_keys):
        lines.append(f"<url><loc>{base}/race/{rk}</loc><changefreq>hourly</changefreq><priority>0.8</priority></url>")
    lines.append("</urlset>")
    return Response(content="\n".join(lines), media_type="application/xml")


@app.get("/robots.txt")
async def robots_txt():
    from fastapi.responses import Response
    base = os.getenv("PUBLIC_BASE_URL", "https://midterm.narve.ai").rstrip("/")
    body = "\n".join([
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin/",
        "Disallow: /premium/",
        f"Sitemap: {base}/sitemap.xml",
        "",
    ])
    return Response(content=body, media_type="text/plain")


@app.get("/og/race/{race_key}.png")
async def og_race_image(race_key: str):
    """Generate an Open Graph share image for a race detail page."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise HTTPException(status_code=503, detail="Pillow not installed")

    # Pull race info
    try:
        race = await data_race_detail(race_key)
    except HTTPException:
        race = {"title": race_key, "by_source": {}}

    img = Image.new("RGB", (1200, 630), color=(250, 250, 249))
    draw = ImageDraw.Draw(img)

    try:
        font_big = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 56)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
    except IOError:
        font_big = ImageFont.load_default()
        font_small = ImageFont.load_default()

    draw.text((60, 60), "MidtermEdge", fill=(120, 113, 108), font=font_small)
    title = (race.get("title") or race_key)[:80]
    draw.text((60, 130), title, fill=(28, 25, 23), font=font_big)

    y = 260
    SRC_COLORS = {"polymarket": (139, 92, 246), "kalshi": (59, 130, 246),
                  "predictit": (245, 158, 11), "polling": (16, 185, 129)}
    for src, m in (race.get("by_source") or {}).items():
        outcomes = m.get("outcomes") or []
        if not outcomes:
            continue
        top = outcomes[0]
        pct = int(round((top.get("probability") or 0) * 100))
        line = f"{src.title()}: {top.get('name', '')}  {pct}%"
        draw.text((60, y), line, fill=SRC_COLORS.get(src, (78, 78, 78)), font=font_small)
        y += 50
        if y > 540:
            break

    from io import BytesIO
    from fastapi.responses import Response
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return Response(content=buf.getvalue(), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/embed/race/{race_key}")
async def embed_race(race_key: str):
    """Standalone iframe-safe single-race widget.

    Renders a minimal HTML page that can be embedded on Substack, Twitter
    cards, etc. No nav, no auth, just the latest cross-source odds.
    """
    from fastapi.responses import HTMLResponse
    try:
        race = await data_race_detail(race_key)
    except HTTPException:
        return HTMLResponse("<p>Race not found</p>", status_code=404)

    rows_html = []
    SRC_COLORS = {"polymarket": "#8b5cf6", "kalshi": "#3b82f6",
                  "predictit": "#f59e0b", "polling": "#10b981"}
    for src, m in (race.get("by_source") or {}).items():
        outcomes = m.get("outcomes") or []
        if not outcomes:
            continue
        top = outcomes[0]
        pct = (top.get("probability") or 0) * 100
        color = SRC_COLORS.get(src, "#78716c")
        name = (top.get('name') or '').replace('<', '&lt;').replace('>', '&gt;')
        rows_html.append(
            f'<div style="display:flex;justify-content:space-between;align-items:center;'
            f'padding:8px 12px;border-bottom:1px solid #f5f5f4">'
            f'<span style="font-weight:600;text-transform:capitalize;color:{color}">{src}</span>'
            f'<span style="color:#57534e;font-size:13px">{name}</span>'
            f'<span style="font-weight:700;color:#1c1917">{pct:.0f}%</span></div>'
        )

    base = os.getenv("PUBLIC_BASE_URL", "https://midterm.narve.ai").rstrip("/")
    title = (race.get("title") or race_key).replace('<', '&lt;').replace('>', '&gt;')
    html = f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<title>{title} — MidtermEdge</title>
<style>
  body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#fafaf9;color:#1c1917}}
  .card{{max-width:480px;margin:0 auto;background:#fff;border:1px solid #e7e5e4;border-radius:12px;overflow:hidden}}
  .head{{padding:12px 16px;border-bottom:1px solid #f5f5f4}}
  .brand{{font-size:11px;color:#a8a29e;text-transform:uppercase;letter-spacing:0.05em}}
  .title{{font-size:15px;font-weight:600;margin-top:4px}}
  .foot{{padding:8px 16px;font-size:11px;color:#a8a29e}}
  a{{color:#0c0a09;text-decoration:none}}
</style>
</head><body>
<div class="card">
  <div class="head"><div class="brand">MidtermEdge</div><div class="title">{title}</div></div>
  {''.join(rows_html) or '<div style="padding:16px;color:#a8a29e">No data</div>'}
  <div class="foot"><a href="{base}/race/{race_key}" target="_blank" rel="noopener">View on MidtermEdge →</a></div>
</div>
</body></html>"""
    return HTMLResponse(html, headers={"Content-Security-Policy": "frame-ancestors *"})


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

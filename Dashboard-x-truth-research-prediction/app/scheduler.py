from __future__ import annotations

import collections
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlmodel import select

from app.config import yaml_config
from app.db import AsyncSession, engine
from app.models import MarketSnapshot, Prediction, RawPost, Source, SourcePredictionRecord

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


async def run_pipeline() -> dict:
    stats = {"posts_fetched": 0, "new_posts": 0, "predictions_extracted": 0, "markets_synced": 0, "resolved": 0, "sources_recomputed": 0, "errors": [], "run_at": datetime.now(timezone.utc).isoformat()}

    async with AsyncSession(engine, expire_on_commit=False) as session:
        # Sync Polymarket
        try:
            from app.markets.polymarket import PolymarketClient
            poly = PolymarketClient()
            new_m, upd_m = await poly.sync_markets(session)
            stats["markets_synced"] = new_m + upd_m
        except Exception as exc:
            logger.error("Polymarket sync failed: %s", exc)
            stats["errors"].append(f"Polymarket sync: {exc}")

        # Sync Kalshi
        try:
            from app.markets.kalshi import KalshiClient
            kalshi = KalshiClient()
            new_k, upd_k = await kalshi.sync_markets(session)
            stats["markets_synced"] += new_k + upd_k
        except Exception as exc:
            logger.error("Kalshi sync failed: %s", exc)
            stats["errors"].append(f"Kalshi sync: {exc}")

        all_posts: list[RawPost] = []
        keywords = yaml_config.get("scraping", {}).get("keywords", {}).get("prediction_keywords", [])
        limit = yaml_config.get("scraping", {}).get("limit_per_source", 100)

        from app.keys_resolver import resolve_truthsocial_creds, resolve_twitter_creds

        try:
            tw_creds = await resolve_twitter_creds()
            from app.scrapers.twitter import TwitterScraper
            tw = TwitterScraper(bearer_token=tw_creds.bearer_token)
            if tw.is_available():
                posts = await tw.fetch(keywords, limit)
                all_posts.extend(posts)
                stats["posts_fetched"] += len(posts)
        except Exception as exc:
            logger.error("Twitter scrape failed: %s", exc)
            stats["errors"].append(f"Twitter: {exc}")

        try:
            ts_creds = await resolve_truthsocial_creds()
            from app.scrapers.truthsocial import TruthSocialScraper
            ts = TruthSocialScraper(
                username=ts_creds.username,
                password=ts_creds.password,
                access_token=ts_creds.access_token,
                api_base_url=ts_creds.api_base_url,
            )
            if ts.is_available():
                posts = await ts.fetch(keywords, limit)
                all_posts.extend(posts)
                stats["posts_fetched"] += len(posts)
        except Exception as exc:
            logger.error("TruthSocial scrape failed: %s", exc)
            stats["errors"].append(f"TruthSocial: {exc}")

        try:
            from app.scrapers.reddit import RedditScraper
            reddit = RedditScraper()
            if reddit.is_available():
                posts = await reddit.fetch(keywords, limit)
                all_posts.extend(posts)
                stats["posts_fetched"] += len(posts)
        except Exception as exc:
            logger.error("Reddit scrape failed: %s", exc)
            stats["errors"].append(f"Reddit: {exc}")

        try:
            from app.scrapers.rss import RSSScraper
            rss = RSSScraper()
            if rss.is_available():
                posts = await rss.fetch(keywords, limit)
                all_posts.extend(posts)
                stats["posts_fetched"] += len(posts)
        except Exception as exc:
            logger.error("RSS scrape failed: %s", exc)
            stats["errors"].append(f"RSS: {exc}")

        for post in all_posts:
            existing = await session.exec(select(RawPost).where(RawPost.id == post.id))
            if existing.first() is None:
                session.add(post)
                stats["new_posts"] += 1
        await session.commit()

        try:
            from app.processing.extractor import PredictionExtractor, match_to_market, _tokenize
            extractor = PredictionExtractor()
            mk_result = await session.exec(select(MarketSnapshot))
            # Tokenize each market question once up front so the inner match loop
            # only pays a set-intersection cost per prediction (was O(P×M) tokenizations).
            # Also bucket by category so match_to_market does an O(1) dict lookup
            # instead of rescanning the whole market list on every prediction.
            markets_by_category: dict[str, list[dict]] = collections.defaultdict(list)
            for ms in mk_result.all():
                question = ms.market_question or ""
                entry = {
                    "market_slug": ms.market_slug,
                    "market_question": question,
                    "category": ms.category,
                    "yes_price": ms.yes_price,
                    "close_time": ms.close_time,
                    # Multi-outcome metadata — fed into match_to_market so it can
                    # gate on the candidate name appearing in the prediction.
                    "event_slug": ms.event_slug,
                    "event_title": ms.event_title,
                    "outcome_name": ms.outcome_name,
                    "_tokens": _tokenize(question),
                }
                markets_by_category[ms.category or "other"].append(entry)

            processed_result = await session.exec(select(Prediction.raw_post_id).distinct())
            processed_ids = set(processed_result.all())
            new_posts_stmt = select(RawPost).where(RawPost.id.notin_(processed_ids)) if processed_ids else select(RawPost)
            new_posts_result = await session.exec(new_posts_stmt)

            for post in new_posts_result.all():
                try:
                    for ext in await extractor.extract_async(post.content):
                        candidate_markets = markets_by_category.get(ext.category, [])
                        matched_market, _ = match_to_market(f"{ext.raw_text} {post.content[:200]}", candidate_markets)
                        market_slug = matched_market["market_slug"] if matched_market else None
                        market_close_time = matched_market.get("close_time") if matched_market else None
                        market_implied_prob = matched_market["yes_price"] if matched_market else None
                        hours_remaining = None
                        if market_close_time and isinstance(market_close_time, datetime):
                            ct = market_close_time.replace(tzinfo=timezone.utc) if market_close_time.tzinfo is None else market_close_time
                            pt = post.posted_at.replace(tzinfo=timezone.utc) if post.posted_at.tzinfo is None else post.posted_at
                            hours_remaining = (ct - pt).total_seconds() / 3600.0
                        counts = hours_remaining is not None and hours_remaining >= 12
                        pred = Prediction(raw_post_id=post.id, market_slug=market_slug, market_question=matched_market.get("market_question") if matched_market else None, market_close_time=market_close_time, hours_remaining_at_prediction=hours_remaining, counts_toward_credibility=counts, category=ext.category, predicted_outcome=ext.predicted_outcome, predicted_probability=ext.predicted_probability, market_implied_probability=market_implied_prob, extracted_at=datetime.now(timezone.utc))
                        session.add(pred)
                        await session.flush()

                        src_result = await session.exec(select(Source).where(Source.handle == post.author_handle))
                        source = src_result.first()
                        if not source:
                            eng = post.engagement
                            source = Source(handle=post.author_handle, platform=post.platform, follower_count=post.follower_count, verified=post.verified, engagement_ratio=min(sum(eng.get(k, 0) for k in eng) / max(post.follower_count, 1), 1.0), last_seen=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc))
                            session.add(source)
                        else:
                            source.last_seen = datetime.now(timezone.utc)
                            source.follower_count = max(source.follower_count, post.follower_count)
                            session.add(source)

                        session.add(SourcePredictionRecord(handle=post.author_handle, prediction_id=pred.id, market_slug=market_slug or "", category=ext.category, predicted_at=post.posted_at, hours_remaining=hours_remaining or 0.0, counted=counts))
                        stats["predictions_extracted"] += 1
                except Exception as exc:
                    logger.error("Extract %s: %s", post.id, exc)
            await session.commit()
        except Exception as exc:
            logger.error("Extraction failed: %s", exc)
            stats["errors"].append(f"Extraction: {exc}")

        try:
            from app.processing.resolver import MarketResolver
            res_stats = await MarketResolver().run(session)
            stats["resolved"] = res_stats.get("predictions_resolved", 0)
        except Exception as exc:
            logger.error("Resolver failed: %s", exc)

        try:
            from app.credibility.engine import CredibilityEngine
            from app.processing.paper_trade import TradeFilter, maybe_open_trade
            from app.processing.ranker import rank_prediction
            stats["sources_recomputed"] = await CredibilityEngine().recompute_all(session)
            unscore_result = await session.exec(select(Prediction).where(Prediction.ev_score.is_(None)))
            trade_filter = TradeFilter()
            opened_trades = []
            for pred in unscore_result.all():
                try:
                    spr_result = await session.exec(select(SourcePredictionRecord).where(SourcePredictionRecord.prediction_id == pred.id))
                    spr = spr_result.first()
                    source = None
                    if spr:
                        src_result = await session.exec(select(Source).where(Source.handle == spr.handle))
                        source = src_result.first()
                    rank_prediction(pred, source)
                    session.add(pred)
                    # Now that the prediction has an EV score and risk flags, decide whether
                    # it qualifies as a paper-trade entry. Stake is fixed at $1/signal.
                    if spr is not None:
                        opened = await maybe_open_trade(session, pred, source, spr.handle, trade_filter)
                        if opened is not None:
                            opened_trades.append(opened)
                except Exception:
                    pass
            stats["paper_trades_opened"] = len(opened_trades)
            await session.commit()
            # Fan-out alerts after commit so we never notify about a trade that
            # rolled back. Best-effort — failures are logged, not raised.
            if opened_trades:
                try:
                    from app.notifications import notify_new_trades
                    sent = await notify_new_trades(opened_trades)
                    stats["telegram_alerts_sent"] = sent
                except Exception as exc:
                    logger.warning("Telegram notifications failed: %s", exc)
        except Exception as exc:
            logger.error("Credibility/scoring failed: %s", exc)

    logger.info("Pipeline: %d posts (%d new), %d preds, %d mkts, %d resolved, %d recomputed, %d errors", stats["posts_fetched"], stats["new_posts"], stats["predictions_extracted"], stats["markets_synced"], stats["resolved"], stats["sources_recomputed"], len(stats["errors"]))
    return stats


async def refresh_open_trade_prices() -> dict:
    """Fast-cadence price refresh for markets with open paper trades.

    Closes the gap between "main pipeline runs every 5 min" and "the price you
    saw 4 minutes ago might already be wrong" without the operational cost of
    a full WebSocket subscription. Runs every 60s — light on the Polymarket
    gamma API, since we only fetch the slugs that have *open* trades, never
    the full universe.

    Side effects:
      - Updates ``MarketSnapshot.yes_price`` for tracked markets.
      - Sends a Telegram drift alert when a market moves ≥10pp against the
        bet's side (signal degraded — operator may want to close manually).
    """
    stats = {"markets_polled": 0, "prices_changed": 0, "drift_alerts_sent": 0, "errors": []}
    async with AsyncSession(engine, expire_on_commit=False) as session:
        from app.models import PaperTrade

        # Distinct (slug, side) for every open paper trade.
        result = await session.exec(
            select(PaperTrade).where(PaperTrade.resolved == False)  # noqa: E712
        )
        open_trades = result.all()
        if not open_trades:
            return stats

        slugs = list({t.market_slug for t in open_trades if t.market_slug})
        if not slugs:
            return stats

        import httpx
        drift_targets: list[tuple[PaperTrade, float, float]] = []  # (trade, old_price, new_yes)

        # Polymarket gamma-api supports filtering by slug. One GET per slug
        # keeps each request small and parallelisable.
        async with httpx.AsyncClient(timeout=10) as client:
            for slug in slugs:
                try:
                    resp = await client.get(
                        "https://gamma-api.polymarket.com/markets",
                        params={"slug": slug, "limit": 1},
                    )
                    if resp.status_code != 200:
                        continue
                    payload = resp.json()
                except Exception as exc:
                    stats["errors"].append(f"{slug}: {exc}")
                    continue
                if not payload:
                    continue
                m = payload[0] if isinstance(payload, list) else payload
                # Parse YES price from the outcomePrices field
                from app.markets.polymarket import PolymarketClient
                prices = PolymarketClient.parse_prices(m)
                yes_price = float(prices[0]) if prices else None
                if yes_price is None:
                    continue
                stats["markets_polled"] += 1

                # Update MarketSnapshot.
                ms_result = await session.exec(
                    select(MarketSnapshot).where(MarketSnapshot.market_slug == slug, MarketSnapshot.platform == "polymarket").order_by(MarketSnapshot.snapshotted_at.desc()).limit(1)
                )
                snap = ms_result.first()
                if snap is not None and abs((snap.yes_price or 0) - yes_price) > 0.001:
                    old = snap.yes_price
                    snap.yes_price = yes_price
                    snap.snapshotted_at = datetime.now(timezone.utc)
                    session.add(snap)
                    stats["prices_changed"] += 1
                    # Check every open trade on this market for 10pp adverse drift.
                    for t in open_trades:
                        if t.market_slug != slug:
                            continue
                        is_yes = (t.bet_side or "YES").upper() == "YES"
                        # Adverse drift = market moves against the bet.
                        moved = (yes_price - t.entry_price) if is_yes else (t.entry_price - yes_price)
                        # Negative = adverse (market moving away from our entry).
                        if moved <= -0.10:
                            drift_targets.append((t, t.entry_price, yes_price))
        await session.commit()

        # Telegram alert for any drifting trades — best-effort.
        if drift_targets:
            try:
                import httpx as _httpx
                from app.notifications import _telegram_subscribers, _post_telegram
                subscribers = await _telegram_subscribers()
                if subscribers:
                    async with _httpx.AsyncClient() as client:
                        for trade, old_price, new_yes in drift_targets:
                            ref_old = old_price
                            ref_new = new_yes if (trade.bet_side or "YES").upper() == "YES" else (1.0 - new_yes)
                            text = (
                                "⚠️ *narve.ai drift alert*\n"
                                f"`{trade.market_slug[:120]}`\n"
                                f"Your *{trade.bet_side}* entry: `{ref_old:.2f}` -> market now `{ref_new:.2f}` "
                                f"(adverse move ≥10pp)\n"
                                f"Source: @{trade.handle}"
                            )
                            for token, chat_id in subscribers:
                                await _post_telegram(client, token, chat_id, text)
                                stats["drift_alerts_sent"] += 1
            except Exception as exc:
                logger.warning("Drift alert send failed: %s", exc)
    logger.info(
        "Price stream: polled=%d, changed=%d, drift alerts=%d",
        stats["markets_polled"], stats["prices_changed"], stats["drift_alerts_sent"],
    )
    return stats


async def _poll_telegram_safe() -> None:
    """Wrap the Telegram poller so any failure is logged, not raised."""
    try:
        from app.telegram_bot import poll_telegram_commands
        await poll_telegram_commands()
    except Exception as exc:
        logger.warning("Telegram poll failed: %s", exc)


def start_scheduler() -> None:
    scheduler.add_job(run_pipeline, trigger=IntervalTrigger(minutes=5), id="main_pipeline", name="Pipeline", replace_existing=True, max_instances=1)
    scheduler.add_job(
        refresh_open_trade_prices,
        trigger=IntervalTrigger(seconds=60),
        id="price_stream",
        name="Price stream",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        _poll_telegram_safe,
        trigger=IntervalTrigger(seconds=15),
        id="telegram_bot",
        name="Telegram bot poller",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info("Scheduler started — pipeline every 5 min, price stream every 60s, telegram poll every 15s")


def shutdown_scheduler() -> None:
    scheduler.shutdown(wait=False)

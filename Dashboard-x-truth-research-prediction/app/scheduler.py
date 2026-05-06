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


def start_scheduler() -> None:
    scheduler.add_job(run_pipeline, trigger=IntervalTrigger(minutes=5), id="main_pipeline", name="Pipeline", replace_existing=True, max_instances=1)
    scheduler.start()
    logger.info("Scheduler started — pipeline every 5 min")


def shutdown_scheduler() -> None:
    scheduler.shutdown(wait=False)

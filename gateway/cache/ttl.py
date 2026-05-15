"""Single-process in-memory TTL cache.

Purpose: keep hot reads off SQLite without standing up Redis. Drop this in
front of query helpers via `cache.get_or_compute(key, factory, ttl)`.

Scope:
  * Single uvicorn worker only. A second worker would serve its own copy;
    cache entries would not be shared. Multi-worker deploys must switch to
    the async Redis-backed `cache/service.py` (separate module, already
    present in this repo) or introduce a broadcast layer.
  * Sync API. Factories must be synchronous — FastAPI handlers call this
    inline and the cache ops themselves are microseconds, so they don't
    block the event loop.
  * Values are stored as-is (no JSON round-trip). Callers are responsible
    for keeping values hashable/picklable if they care; we just hand the
    same reference back. Don't mutate what comes out of the cache.

Key-schema convention (canonical — change in one place, update docs):
  feed:user_{uid}:cat_{cat}:sort_{sort}:page_{page}         ttl=60
  best_bets:tier_{tier}:page_{page}                         ttl=120
  markets:cat_{cat}:sort_{sort}:page_{page}                 ttl=30
  market:{slug}                                             ttl=30
  source:{handle}                                           ttl=300
  sources:sort_{sort}:filter_{filter}:page_{page}           ttl=120
  source_history:{handle}                                   ttl=300
  source_network                                            ttl=600
  market_chart:{slug}                                       ttl=120
  insider_signals:type_{type}:days_{days}:page_{page}       ttl=120
  insider_leaderboard                                       ttl=600
  og_card:default                                           ttl=3600
  og_card:source:{handle}                                   ttl=3600
  og_card:market:{slug}                                     ttl=3600
  credibility_consensus:{slug}                              ttl=60

Invalidate via `cache.delete(key)` for single-key writes or
`cache.delete_prefix("feed:")` when a write fans out across a namespace.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any, Callable, Optional


# Default TTLs keyed by spec prefix, so callers can reach them without
# hard-coding the number twice. Unknown prefix → caller must pass ttl.
DEFAULT_TTLS: dict[str, int] = {
    "feed": 60,
    "best_bets": 120,
    "markets": 30,
    "market": 30,
    "source": 300,
    "sources": 120,
    "source_history": 300,
    "source_network": 600,
    "market_chart": 120,
    "insider_signals": 120,
    "insider_leaderboard": 600,
    "og_card": 3600,
    "credibility_consensus": 60,
}


def _prefix(key: str) -> str:
    """First colon-segment of a key, for hit-rate attribution."""
    i = key.find(":")
    return key if i < 0 else key[:i]


class TTLCache:
    """Thread-safe TTL cache with bounded size + per-prefix hit counters.

    Eviction: when `max_items` is reached we drop the entry with the
    lowest expiry timestamp (closest to expiring). That's roughly LRU-by-
    remaining-life; cheap to compute and acceptable for a cache this size.
    """

    def __init__(self, max_items: int = 10_000):
        # (expires_at_epoch_seconds, value)
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.RLock()
        self._max = max_items

        # Stats. Per-prefix counters let /admin/cache surface hot keys.
        self._hits: dict[str, int] = defaultdict(int)
        self._misses: dict[str, int] = defaultdict(int)
        self._sets: dict[str, int] = defaultdict(int)
        self._evictions = 0

    # ── core ops ────────────────────────────────────────────────────────

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self._misses[_prefix(key)] += 1
                return None
            expires_at, value = entry
            if time.time() > expires_at:
                self._data.pop(key, None)
                self._misses[_prefix(key)] += 1
                return None
            self._hits[_prefix(key)] += 1
            return value

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            ttl_seconds = 60
        with self._lock:
            if key not in self._data and len(self._data) >= self._max:
                # Evict entry with soonest expiry. O(n) but n is bounded.
                victim_key = min(self._data, key=lambda k: self._data[k][0])
                self._data.pop(victim_key, None)
                self._evictions += 1
            self._data[key] = (time.time() + ttl_seconds, value)
            self._sets[_prefix(key)] += 1

    def delete(self, key: str) -> int:
        """Returns 1 if the key existed, 0 otherwise — matches Redis shape."""
        with self._lock:
            return 1 if self._data.pop(key, None) is not None else 0

    def delete_prefix(self, prefix: str) -> int:
        """Remove every key whose name starts with `prefix`.

        Returns count removed. Called from write-side invalidation.
        """
        with self._lock:
            victims = [k for k in self._data if k.startswith(prefix)]
            for k in victims:
                self._data.pop(k, None)
            return len(victims)

    def clear(self) -> int:
        """Drop everything. Used by the /admin/cache clear button + tests."""
        with self._lock:
            n = len(self._data)
            self._data.clear()
            return n

    def get_or_compute(
        self,
        key: str,
        factory: Callable[[], Any],
        ttl_seconds: int,
    ) -> Any:
        """Fetch-through helper. Factory exceptions propagate (don't cache errors).

        Beware: the factory runs outside the lock to avoid stalling other
        readers on a slow DB query. A racing second caller may run the
        factory too; last-writer-wins on set(). That's acceptable — it
        only costs a duplicate query on the very first miss.
        """
        cached = self.get(key)
        if cached is not None:
            return cached
        value = factory()
        self.set(key, value, ttl_seconds)
        return value

    # ── stats / introspection for /admin/cache ──────────────────────────

    def stats(self) -> dict[str, Any]:
        """Point-in-time snapshot. Cheap enough to call per-request."""
        with self._lock:
            now = time.time()
            total = len(self._data)
            expired = sum(1 for expires_at, _ in self._data.values() if now > expires_at)
            hits = dict(self._hits)
            misses = dict(self._misses)
            sets = dict(self._sets)
            evictions = self._evictions

        total_hits = sum(hits.values())
        total_misses = sum(misses.values())
        attempts = total_hits + total_misses
        hit_rate = round(total_hits / attempts, 4) if attempts else 0.0

        prefixes = sorted(set(hits) | set(misses) | set(sets))
        per_prefix = []
        for p in prefixes:
            h = hits.get(p, 0)
            m = misses.get(p, 0)
            a = h + m
            per_prefix.append({
                "prefix": p,
                "hits": h,
                "misses": m,
                "sets": sets.get(p, 0),
                "hit_rate": round(h / a, 4) if a else 0.0,
            })

        return {
            "total": total,
            "expired": expired,
            "live": total - expired,
            "max_items": self._max,
            "evictions": evictions,
            "total_hits": total_hits,
            "total_misses": total_misses,
            "hit_rate": hit_rate,
            "per_prefix": per_prefix,
        }

    def reset_stats(self) -> None:
        """Zero hit/miss/set counters. Tests rely on this."""
        with self._lock:
            self._hits.clear()
            self._misses.clear()
            self._sets.clear()
            self._evictions = 0


# Module-level singleton. Single cache per process.
ttl_cache = TTLCache()


# ── Invalidation facade ──────────────────────────────────────────────────
#
# Write sites import `ttl_invalidate` so the key schema stays in one place.
# When a new endpoint is cached, teach the helper here and every existing
# writer picks it up for free. The name `invalidate` is reserved for the
# async Redis-backed cache (cache/service.py).


class ttl_invalidate:
    """Namespace of invalidation helpers for the sync TTL cache."""

    @staticmethod
    def on_new_prediction(handle: str, market_slug: str) -> int:
        """A source published a new prediction — the feed, that source's
        profile, its history, and the affected market's consensus can all
        shift. Best-bets rankings depend on credibility too, so wipe those.
        """
        removed = 0
        removed += ttl_cache.delete_prefix("feed:")
        removed += ttl_cache.delete_prefix("best_bets:")
        removed += ttl_cache.delete(f"source:{handle}")
        removed += ttl_cache.delete(f"source_history:{handle}")
        removed += ttl_cache.delete(f"credibility_consensus:{market_slug}")
        return removed

    @staticmethod
    def on_market_resolved(slug: str) -> int:
        """Market went YES/NO — flush market-scoped keys AND the feed
        (resolution ribbons change what users see).

        Also bust the OG card. The card embeds the live narve-vs-market
        probabilities; resolution flips the headline number to 0% / 100%,
        and a stale card would show pre-resolution odds for the next
        hour. The card key in the TTL cache is namespaced
        ``og_card:og:market:{slug}`` (og_cards.cached() prefixes
        ``og_card:`` onto the ``og:market:{slug}`` key the route passes
        in). Use a prefix delete so any future per-variant suffixes
        (`:v2`, `:locale_en`) also get nuked.
        """
        removed = 0
        removed += ttl_cache.delete(f"market:{slug}")
        removed += ttl_cache.delete(f"market_chart:{slug}")
        removed += ttl_cache.delete_prefix("feed:")
        removed += ttl_cache.delete(f"credibility_consensus:{slug}")
        removed += ttl_cache.delete_prefix(f"og_card:og:market:{slug}")
        return removed

    @staticmethod
    def on_credibility_recompute() -> int:
        """Nightly cron finished rescoring sources — invalidate the whole
        source namespace + the network graph.

        Also bust every per-source OG card. The card prints the credibility
        score, accuracy, and prediction count straight from the source
        credibility table; after a recompute the displayed numbers are
        wrong until the 1-hour TTL expires. Full-prefix wipe because we
        don't know which sources moved.
        """
        removed = 0
        removed += ttl_cache.delete_prefix("source:")
        removed += ttl_cache.delete_prefix("source_history:")
        removed += ttl_cache.delete_prefix("sources:")
        removed += ttl_cache.delete("source_network")
        removed += ttl_cache.delete_prefix("og_card:og:source:")
        return removed

    @staticmethod
    def on_subscription_change(user_id: int) -> int:
        """User's tier changed — their feed gate AND best-bets tier-scope
        both need a reset. best_bets is global but tier-keyed, so flush all
        of it rather than guess which tier they moved to/from.

        Also fire-and-forgets the async (Redis-backed) keys that cache the
        per-user landing surfaces — /dashboards, /settings, /signal-search.
        Their TTLs are short (30-60s) so a missed bust self-heals, but
        wiring it here means subscribe/unsubscribe reflects immediately.
        """
        removed = 0
        removed += ttl_cache.delete_prefix(f"feed:user_{user_id}:")
        removed += ttl_cache.delete_prefix("best_bets:")

        # Async cache invalidation. Imported locally so an absent cache.service
        # module never breaks the sync helper. Schedule via the running event
        # loop if we're already inside one (FastAPI handlers, ARQ workers);
        # fall back to a transient loop for sync test scripts.
        try:
            import asyncio  # local import — keep this module dep-free at top.
            from cache.service import cache as _async_cache

            async def _bust() -> None:
                await _async_cache.delete(f"dashboards:user:{user_id}")
                await _async_cache.delete(f"settings:user:{user_id}")
                await _async_cache.delete(f"signal_search:user:{user_id}")

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(_bust())
                else:
                    loop.run_until_complete(_bust())
            except RuntimeError:
                # No event loop at all (rare — sync tests). Drive a transient
                # one so the bust still lands.
                asyncio.run(_bust())
        except Exception:
            # Cache module missing or broken — TTL-based self-heal covers it.
            pass

        return removed

    @staticmethod
    def on_role_change(user_id: int) -> int:
        """User's role changed (user ↔ admin ↔ super-admin) — bust the per-user
        landing-surface async caches so any admin-only payload differences
        reflect immediately on the next request.

        Today's `/dashboards`, `/settings`, and `/signal-search` payloads don't
        embed role-gated fields, so a missed bust is not a data leak — but
        wiring this here matches the pattern of `on_subscription_change` and
        means future admin-only fields cannot leak across a demotion via a
        stale cache entry. The sync TTL cache (`feed:*`, `best_bets:*`) is
        not user-role-keyed, so we don't touch it.

        Returns the number of sync-cache keys removed (always 0 today — the
        async deletes are fire-and-forget through the same path used by
        `on_subscription_change`).
        """
        # Reuse the async-bust block. Identical key set to the subscription
        # helper because both events change what the per-user surface should
        # render. Keep this code path in lock-step with on_subscription_change.
        try:
            import asyncio
            from cache.service import cache as _async_cache

            async def _bust() -> None:
                await _async_cache.delete(f"dashboards:user:{user_id}")
                await _async_cache.delete(f"settings:user:{user_id}")
                await _async_cache.delete(f"signal_search:user:{user_id}")

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(_bust())
                else:
                    loop.run_until_complete(_bust())
            except RuntimeError:
                asyncio.run(_bust())
        except Exception:
            # Async cache missing/broken — TTL-based self-heal (30-60s) covers it.
            pass

        return 0

    @staticmethod
    def on_feature_flag_change() -> int:
        """Flags gate what rows the feed materialises — conservative wipe."""
        return ttl_cache.delete_prefix("feed:")

    @staticmethod
    def everything() -> int:
        """Nuclear. /admin/cache clear button."""
        return ttl_cache.clear()

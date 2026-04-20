"""Real-time market movement detection engine.

Detects five event types by comparing current market state against recent
snapshots stored in market_snapshots:

  1. odds_movement   — price swings exceeding a threshold
  2. volume_spike    — volume jumps relative to recent average
  3. new_market      — markets with no prior snapshot
  4. approaching_resolution — close_time within N hours
  5. reversal        — price reverses direction after a prior move

Each detected event is persisted to market_movement_events (via db helpers)
and later matched against user_market_alerts for delivery.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("movement_detector")

# ── Severity thresholds ─────────────────────────────────────────────────────

PRICE_THRESHOLDS = {
    "low": 0.05,
    "medium": 0.10,
    "high": 0.20,
    "critical": 0.35,
}

VOLUME_SPIKE_MULTIPLIER = 3.0  # current vs avg → "medium"
VOLUME_SPIKE_CRITICAL = 8.0

APPROACHING_HOURS = {
    "low": 48,
    "medium": 24,
    "high": 12,
    "critical": 4,
}


@dataclass
class MovementEvent:
    event_type: str
    market_slug: str
    market_question: Optional[str] = None
    category: Optional[str] = None
    source_platform: str = "polymarket"
    old_price: Optional[float] = None
    new_price: Optional[float] = None
    price_change: Optional[float] = None
    old_volume: Optional[float] = None
    new_volume: Optional[float] = None
    volume_change: Optional[float] = None
    close_time: Optional[int] = None
    hours_to_close: Optional[float] = None
    severity: str = "medium"
    metadata: dict = field(default_factory=dict)
    detected_at: int = 0


class MarketMovementDetector:
    """Stateless detector — call detect() with current markets each cycle."""

    def __init__(
        self,
        *,
        price_threshold: float = 0.08,
        lookback_seconds: int = 7200,
        volume_spike_mult: float = VOLUME_SPIKE_MULTIPLIER,
        approaching_hours: float = 24.0,
        reversal_min_swing: float = 0.10,
    ):
        self.price_threshold = price_threshold
        self.lookback_seconds = lookback_seconds
        self.volume_spike_mult = volume_spike_mult
        self.approaching_hours = approaching_hours
        self.reversal_min_swing = reversal_min_swing

    def detect(self, markets, *, now: Optional[int] = None) -> list[MovementEvent]:
        """Run all detectors against a list of UnifiedMarket objects.

        Returns a list of MovementEvent instances (not yet persisted).
        """
        import db

        now = now or int(time.time())
        lookback_ts = now - self.lookback_seconds
        events: list[MovementEvent] = []

        for m in markets:
            if m.status != "active":
                continue

            slug = m.id.split(":", 1)[1] if ":" in m.id else m.id
            source = m.id.split(":", 1)[0] if ":" in m.id else "polymarket"

            old_snap = db.get_market_snapshot_at(slug, lookback_ts)

            # 1. New market — no prior snapshot at all
            if old_snap is None:
                latest = db.get_latest_market_snapshot(slug)
                if latest is None:
                    events.append(MovementEvent(
                        event_type="new_market",
                        market_slug=slug,
                        market_question=m.title,
                        category=m.category,
                        source_platform=source,
                        new_price=m.yes_price,
                        new_volume=m.volume_usd,
                        severity="low",
                        metadata={
                            "volume_usd": m.volume_usd,
                            "liquidity_usd": m.liquidity_usd,
                        },
                        detected_at=now,
                    ))
                continue

            old_price = old_snap["yes_price"]
            price_change = m.yes_price - old_price

            # 2. Odds movement
            if abs(price_change) >= self.price_threshold:
                severity = _price_severity(abs(price_change))
                events.append(MovementEvent(
                    event_type="odds_movement",
                    market_slug=slug,
                    market_question=m.title,
                    category=m.category,
                    source_platform=source,
                    old_price=old_price,
                    new_price=m.yes_price,
                    price_change=price_change,
                    severity=severity,
                    metadata={
                        "direction": "up" if price_change > 0 else "down",
                        "betyc_consensus": m.betyc_consensus,
                        "betyc_ev_score": m.betyc_ev_score,
                        "prediction_count": m.betyc_prediction_count,
                    },
                    detected_at=now,
                ))

            # 3. Volume spike
            try:
                old_vol = old_snap["volume"]
            except (IndexError, KeyError):
                old_vol = None
            if old_vol and old_vol > 0 and m.volume_usd > 0:
                vol_ratio = m.volume_usd / old_vol
                if vol_ratio >= self.volume_spike_mult:
                    vol_severity = "critical" if vol_ratio >= VOLUME_SPIKE_CRITICAL else "medium"
                    events.append(MovementEvent(
                        event_type="volume_spike",
                        market_slug=slug,
                        market_question=m.title,
                        category=m.category,
                        source_platform=source,
                        old_volume=old_vol,
                        new_volume=m.volume_usd,
                        volume_change=vol_ratio,
                        severity=vol_severity,
                        metadata={"volume_ratio": round(vol_ratio, 2)},
                        detected_at=now,
                    ))

            # 4. Approaching resolution
            if m.close_time:
                try:
                    close_ts = _to_unix_ts(m.close_time)
                    if close_ts and close_ts > now:
                        hours_left = (close_ts - now) / 3600
                        if hours_left <= self.approaching_hours:
                            sev = _approaching_severity(hours_left)
                            events.append(MovementEvent(
                                event_type="approaching_resolution",
                                market_slug=slug,
                                market_question=m.title,
                                category=m.category,
                                source_platform=source,
                                new_price=m.yes_price,
                                close_time=close_ts,
                                hours_to_close=round(hours_left, 1),
                                severity=sev,
                                metadata={
                                    "hours_to_close": round(hours_left, 1),
                                    "current_price": m.yes_price,
                                },
                                detected_at=now,
                            ))
                except (ValueError, TypeError):
                    pass

            # 5. Reversal detection — needs two prior snapshots
            if abs(price_change) >= self.reversal_min_swing:
                even_older_ts = lookback_ts - self.lookback_seconds
                even_older_snap = db.get_market_snapshot_at(slug, even_older_ts)
                if even_older_snap:
                    prev_change = old_price - even_older_snap["yes_price"]
                    if abs(prev_change) >= self.reversal_min_swing:
                        # Direction reversed
                        if (prev_change > 0 and price_change < 0) or (prev_change < 0 and price_change > 0):
                            events.append(MovementEvent(
                                event_type="reversal",
                                market_slug=slug,
                                market_question=m.title,
                                category=m.category,
                                source_platform=source,
                                old_price=old_price,
                                new_price=m.yes_price,
                                price_change=price_change,
                                severity="high",
                                metadata={
                                    "prior_change": round(prev_change, 4),
                                    "current_change": round(price_change, 4),
                                    "reversal_magnitude": round(abs(price_change) + abs(prev_change), 4),
                                },
                                detected_at=now,
                            ))

        return events


def persist_events(events: list[MovementEvent]) -> list[int]:
    """Write detected events to market_movement_events. Returns inserted IDs."""
    import db

    ids = []
    with db.conn() as c:
        for ev in events:
            cur = c.execute(
                "INSERT INTO market_movement_events "
                "(event_type, market_slug, market_question, category, source_platform, "
                "old_price, new_price, price_change, old_volume, new_volume, "
                "volume_change, close_time, hours_to_close, severity, "
                "metadata_json, detected_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ev.event_type, ev.market_slug, ev.market_question,
                    ev.category, ev.source_platform,
                    ev.old_price, ev.new_price, ev.price_change,
                    ev.old_volume, ev.new_volume, ev.volume_change,
                    ev.close_time, ev.hours_to_close, ev.severity,
                    json.dumps(ev.metadata), ev.detected_at,
                ),
            )
            ids.append(cur.lastrowid)
    return ids


def deduplicate(events: list[MovementEvent], cooldown_seconds: int = 1800) -> list[MovementEvent]:
    """Drop events if the same (event_type, market_slug) was detected recently."""
    import db

    cutoff = int(time.time()) - cooldown_seconds
    result = []
    with db.conn() as c:
        for ev in events:
            row = c.execute(
                "SELECT 1 FROM market_movement_events "
                "WHERE event_type = ? AND market_slug = ? AND detected_at > ? "
                "LIMIT 1",
                (ev.event_type, ev.market_slug, cutoff),
            ).fetchone()
            if not row:
                result.append(ev)
    return result


# ── Helpers ──────────────────────────────────────────────────────────────────


def _price_severity(abs_change: float) -> str:
    if abs_change >= PRICE_THRESHOLDS["critical"]:
        return "critical"
    if abs_change >= PRICE_THRESHOLDS["high"]:
        return "high"
    if abs_change >= PRICE_THRESHOLDS["medium"]:
        return "medium"
    return "low"


def _approaching_severity(hours_left: float) -> str:
    if hours_left <= APPROACHING_HOURS["critical"]:
        return "critical"
    if hours_left <= APPROACHING_HOURS["high"]:
        return "high"
    if hours_left <= APPROACHING_HOURS["medium"]:
        return "medium"
    return "low"


def _to_unix_ts(val) -> Optional[int]:
    """Convert int, float, numeric string, or ISO-8601 string → Unix timestamp."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip()
    # Try as a plain number first
    try:
        return int(float(s))
    except (ValueError, OverflowError):
        pass
    # Try ISO-8601
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None

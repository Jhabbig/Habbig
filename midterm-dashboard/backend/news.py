"""Political news ingestion + market-reaction measurement.

Two responsibilities:

  1. **Fetch** curated political RSS feeds, tag each headline to a midterm
     race (when the headline mentions a recognised state / candidate /
     office), and persist to ``midterm_news_events``.
  2. **Measure** the market reaction to each tagged news event by joining
     against ``midterm_price_history``. For each market in the tagged race,
     compare the price at the snapshot just before the news to the price at
     the next snapshot(s) within REACTION_WINDOW_SECONDS. Output is the
     largest movement and the lag (time from publish → first material move).

The lag-curve endpoint aggregates these reactions to surface "median time
for source X to reprice after a major news drop" — a metric no paid
election tracker exposes today.
"""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.request
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Optional

try:
    from defusedxml.ElementTree import fromstring as _xml_fromstring
except ImportError:  # pragma: no cover — defusedxml is in requirements.txt
    from xml.etree.ElementTree import fromstring as _xml_fromstring

from data_sources.fips import STATE_NAMES, STATE_FIPS

logger = logging.getLogger(__name__)

# Curated political RSS feeds. Each one is free + public. If a feed dies the
# tagger keeps working on the others. Adding a feed is a one-line change.
NEWS_FEEDS: list[dict] = [
    {"name": "AP Politics",        "url": "https://feeds.apnews.com/rss/apf-politics"},
    {"name": "Politico",           "url": "https://www.politico.com/rss/politics08.xml"},
    {"name": "The Hill",           "url": "https://thehill.com/feed/"},
    {"name": "Reuters Politics",   "url": "https://www.reuters.com/politics/rss"},
    {"name": "NPR Politics",       "url": "https://feeds.npr.org/1014/rss.xml"},
    {"name": "ABC Politics",       "url": "https://abcnews.go.com/abcnews/politicsheadlines"},
]

# How wide a window after the news publish timestamp we look at to find the
# market's reaction. 6h is generous enough to capture all but the slowest
# repricings while still attributing the move to this specific news event.
REACTION_WINDOW_SECONDS = 6 * 3600

# Minimum |delta| in absolute probability terms for a snapshot to count as
# a "material move" (used for lag measurement). 1pp avoids attributing
# small drift to news.
REACTION_THRESHOLD = 0.01


# Word-boundary regexes built once at import time. Office keywords map to
# race types and pluralise consistently.
_OFFICE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bsenate\b|\bsenator\b|\bsen\.\b",   re.I), "senate"),
    (re.compile(r"\bhouse\b|\brepresentative\b|\brep\.\b", re.I), "house"),
    (re.compile(r"\bgovernor\b|\bgubernatorial\b",     re.I), "governor"),
    (re.compile(r"\bpresident(ial)?\b",                 re.I), "presidential"),
    (re.compile(r"\bprimary\b|\bnomination\b",          re.I), "primary"),
]

# Optional politician → state shortcuts. Keep small; the per-state name match
# does most of the work. These cover the most-mentioned 2026 candidates that
# headline writers shorten ("Vance" without "Ohio").
_POLITICIAN_TO_STATE: dict[str, str] = {
    "vance": "OH",
    "fetterman": "PA",
    "ossoff": "GA",
    "warnock": "GA",
    "tester": "MT",
    "manchin": "WV",
    "shapiro": "PA",
    "youngkin": "VA",
    "newsom": "CA",
    "desantis": "FL",
    "abbott": "TX",
    "cruz": "TX",
    "cornyn": "TX",
    "scott": "FL",
    "rubio": "FL",
    "schumer": "NY",
    "mcconnell": "KY",
}


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_pubdate(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def fetch_rss_feed(url: str, user_agent: str = "narve.ai/midterm-dashboard") -> list[dict]:
    """Fetch one RSS feed and return raw item dicts. Synchronous — call
    via ``asyncio.to_thread`` from the background loop."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml_data = resp.read()
    except Exception as e:
        logger.info(f"RSS fetch failed for {url}: {e}")
        return []

    try:
        root = _xml_fromstring(xml_data)
    except Exception as e:
        logger.info(f"RSS parse failed for {url}: {e}")
        return []

    items = root.findall(".//item")
    if not items:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        items = root.findall("a:entry", ns)

    out: list[dict] = []
    for item in items[:40]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (
            item.findtext("pubDate")
            or item.findtext("{http://purl.org/dc/elements/1.1/}date")
            or ""
        ).strip()
        desc = _strip_html(item.findtext("description") or "")[:280]
        if not title:
            continue
        if link and not link.startswith(("http://", "https://")):
            link = ""
        out.append({
            "title": title,
            "link": link,
            "pub_date": pub_date,
            "description": desc,
        })
    return out


def tag_article(title: str, description: str = "") -> dict:
    """Identify which race a headline is about.

    Returns a dict::

        {
            "race_key": "senate_TX" | None,
            "state": "TX" | None,
            "race_type": "senate" | None,
            "keywords": ["senate", "texas", ...],
        }

    The matching is keyword-based and deliberately strict so we don't tag a
    "Trump rally in Florida" piece as a Florida-Senate news event. We
    require at least one office + one state-resolving signal.
    """
    if not title:
        return {"race_key": None, "state": None, "race_type": None, "keywords": []}

    combined = f"{title} {description}".lower()
    keywords: list[str] = []

    # 1. Office detection
    race_type: Optional[str] = None
    for pat, rt in _OFFICE_PATTERNS:
        if pat.search(combined):
            race_type = rt
            keywords.append(rt)
            break

    # 2. State detection — full state names first (word boundary). Then
    #    politician shortcuts. Then 2-letter codes for unambiguous ones.
    state: Optional[str] = None
    sorted_states = sorted(STATE_NAMES.items(), key=lambda kv: len(kv[1]), reverse=True)
    is_dc = "washington d.c." in combined or "washington, d.c." in combined
    for abbr, name in sorted_states:
        nl = name.lower()
        if nl == "washington" and is_dc:
            continue
        if re.search(rf"\b{re.escape(nl)}\b", combined):
            state = abbr
            keywords.append(nl)
            break

    if not state:
        for person, abbr in _POLITICIAN_TO_STATE.items():
            if re.search(rf"\b{re.escape(person)}\b", combined):
                state = abbr
                keywords.append(person)
                break

    if not state:
        # Conservative postal-code match: only abbrs that aren't English words.
        ambiguous = {"IN", "OR", "ME", "OH", "AL", "OK", "HI", "ID", "PA", "MA", "AK", "AR", "DE"}
        padded = f" {title} "
        for abbr in STATE_FIPS:
            if abbr in ambiguous:
                continue
            if f" {abbr} " in padded:
                state = abbr
                break

    if not race_type or not state:
        return {"race_key": None, "state": state, "race_type": race_type, "keywords": keywords}

    return {
        "race_key": f"{race_type}_{state}",
        "state": state,
        "race_type": race_type,
        "keywords": keywords,
    }


async def ingest_news(db) -> int:
    """Fetch every configured feed, tag each item, persist new ones to the DB.

    Returns the number of new items inserted (after dedupe).
    """
    inserted = 0
    for feed in NEWS_FEEDS:
        items = await asyncio.to_thread(fetch_rss_feed, feed["url"])
        for item in items:
            pub_dt = _parse_pubdate(item["pub_date"])
            if not pub_dt:
                continue
            tag = tag_article(item["title"], item.get("description", ""))
            row_id = db.upsert_news_event(
                source=feed["name"],
                title=item["title"],
                link=item.get("link"),
                description=item.get("description"),
                published_at=pub_dt.isoformat(),
                race_key=tag.get("race_key"),
                state=tag.get("state"),
                keywords=tag.get("keywords"),
            )
            if row_id is not None:
                inserted += 1
    if inserted:
        logger.info(f"News ingest: {inserted} new items")
    return inserted


def compute_market_reaction(
    *,
    snapshots_before: list[dict],
    snapshots_after: list[dict],
    news_published_at: datetime,
) -> Optional[dict]:
    """Given price snapshots straddling a news event, infer the reaction.

    Each snapshot has shape ``{"timestamp": iso8601, "prices": {outcome: p, ...}}``.
    We pick the most recent "before" price as baseline, then walk the
    "after" snapshots looking for the first one whose top-outcome price
    moves more than ``REACTION_THRESHOLD`` away. Returns ``None`` if there
    isn't enough data on either side.
    """
    if not snapshots_before or not snapshots_after:
        return None

    def _top_price(prices) -> Optional[float]:
        if isinstance(prices, dict):
            try:
                vals = [float(v) for v in prices.values() if v is not None]
                return max(vals) if vals else None
            except (TypeError, ValueError):
                return None
        if isinstance(prices, list) and prices:
            try:
                return max(float(v) for v in prices if v is not None)
            except (TypeError, ValueError):
                return None
        return None

    baseline_snap = snapshots_before[-1]  # most recent before
    baseline = _top_price(baseline_snap.get("prices"))
    if baseline is None:
        return None

    reaction_price = baseline
    lag_seconds: Optional[int] = None
    max_delta = 0.0
    for snap in snapshots_after:
        p = _top_price(snap.get("prices"))
        if p is None:
            continue
        delta = abs(p - baseline)
        if delta > max_delta:
            max_delta = delta
            reaction_price = p
        if delta >= REACTION_THRESHOLD and lag_seconds is None:
            try:
                ts = datetime.fromisoformat(snap["timestamp"].replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                lag_seconds = max(0, int((ts - news_published_at).total_seconds()))
            except (KeyError, ValueError, TypeError):
                pass

    return {
        "baseline_price": round(baseline, 4),
        "reaction_price": round(reaction_price, 4),
        "delta_pp": round(max_delta * 100, 3),
        "lag_seconds": lag_seconds,
    }


async def measure_reactions(db) -> int:
    """For each unprocessed news event with a race_key, compute the market
    reaction. Returns the number of reaction rows written.

    The matcher works at the market-id level: we look up every market that
    contributed to the news's race_key (via the canonical race-key derivation)
    and join the price-history. Each market produces one reaction row.
    """
    from main import market_race_key  # local import — main imports this module

    pending = db.get_news_needing_reaction(max_age_hours=24)
    if not pending:
        return 0

    # Cache the full list of polymarket markets (rare enough that we don't
    # need to query per-news).
    all_markets = db.get_all_markets(active_only=False)
    by_race: dict[str, list[dict]] = {}
    for m in all_markets:
        rk = market_race_key(m)
        by_race.setdefault(rk, []).append(m)

    written = 0
    for ev in pending:
        rk = ev.get("race_key")
        if not rk:
            continue
        try:
            published_at = datetime.fromisoformat(ev["published_at"].replace("Z", "+00:00"))
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
        except (ValueError, KeyError, TypeError):
            continue

        # Don't try to measure reactions to news that's still inside the
        # reaction window — wait until enough time has passed for the market
        # to have moved.
        if datetime.now(timezone.utc) - published_at < timedelta(minutes=15):
            continue

        candidate_markets = by_race.get(rk, [])
        if not candidate_markets:
            continue

        start = (published_at - timedelta(hours=2)).isoformat()
        end = (published_at + timedelta(seconds=REACTION_WINDOW_SECONDS)).isoformat()

        for m in candidate_markets:
            mid = m.get("id")
            if mid is None:
                continue
            snaps = db.get_price_snapshots_for_market(
                market_id=mid, start=start, end=end,
            )
            before = [s for s in snaps if s["timestamp"] <= published_at.isoformat()]
            after = [s for s in snaps if s["timestamp"] > published_at.isoformat()]
            reaction = compute_market_reaction(
                snapshots_before=before,
                snapshots_after=after,
                news_published_at=published_at,
            )
            if reaction is None:
                continue
            db.record_news_reaction(
                news_id=ev["id"],
                source=m.get("source", "unknown"),
                market_id=mid,
                race_key=rk,
                baseline_price=reaction["baseline_price"],
                reaction_price=reaction["reaction_price"],
                lag_seconds=reaction["lag_seconds"],
            )
            written += 1

    if written:
        logger.info(f"News reactions recorded: {written}")
    return written


def lag_curve(reactions: list[dict], *, min_delta_pp: float = 1.0) -> dict:
    """Aggregate per-source lag statistics from a list of reaction rows.

    Returns::

        {
          "by_source": {
            "polymarket": {"median_lag_s": 480, "n": 23, "median_delta_pp": 2.1},
            ...
          },
          "n_total": 23,
        }
    """
    by_src: dict[str, list[dict]] = {}
    for r in reactions:
        if (r.get("delta_pp") or 0) < min_delta_pp:
            continue
        if r.get("lag_seconds") is None:
            continue
        by_src.setdefault(r["source"], []).append(r)

    def _median(values: list[float]) -> Optional[float]:
        if not values:
            return None
        s = sorted(values)
        n = len(s)
        mid = n // 2
        if n % 2:
            return s[mid]
        return (s[mid - 1] + s[mid]) / 2

    out = {}
    for src, items in by_src.items():
        lags = [r["lag_seconds"] for r in items]
        deltas = [r["delta_pp"] for r in items]
        out[src] = {
            "median_lag_s": _median(lags),
            "median_delta_pp": _median(deltas),
            "n": len(items),
        }

    return {"by_source": out, "n_total": sum(len(v) for v in by_src.values())}

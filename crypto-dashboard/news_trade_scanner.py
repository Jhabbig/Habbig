#!/usr/bin/env python3
"""
News-Trade Correlation Scanner (v2)

LOGIC: Trades first, news second.

1. Pull flagged suspicious trades from the existing scanner (all odds, not
   just long shots — a $50K bet at 40% odds is just as suspicious if news
   validates it 2 hours later).
2. Extract the market topics from those trades (e.g. "Iran", "Trump",
   "Fed rate", "Bitcoin").
3. Scan breaking news for events that MATCH those market topics.
4. When a news event correlates with a previously flagged trade, score the
   correlation: how big was the bet, how soon before the news, how much
   would the bettor profit.
5. These are the real insider-trade alerts.

Runs every 20 minutes from the server background task.
"""

from __future__ import annotations

import re
import json
import time
import hashlib
import tempfile
import os
import defusedxml.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

import requests

# ─── Config ───────────────────────────────────────────────────────────
CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

# RSS feeds for breaking news
NEWS_FEEDS = [
    ("https://feeds.bbci.co.uk/news/world/rss.xml", "BBC World"),
    ("https://feeds.bbci.co.uk/news/business/rss.xml", "BBC Business"),
    ("https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "NYT World"),
    ("https://rss.nytimes.com/services/xml/rss/nyt/Business.xml", "NYT Business"),
    ("https://feeds.reuters.com/reuters/topNews", "Reuters"),
    ("https://feeds.reuters.com/reuters/businessNews", "Reuters Biz"),
    ("https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US", "Yahoo Finance"),
    ("https://www.cnbc.com/id/100003114/device/rss/rss.html", "CNBC"),
    ("https://cointelegraph.com/rss", "CoinTelegraph"),
    ("https://www.coindesk.com/arc/outboundfeeds/rss/", "CoinDesk"),
    ("https://feeds.arstechnica.com/arstechnica/index", "Ars Technica"),
]

# Common words to ignore when matching trade topics to news
STOP_WORDS = frozenset({
    "the", "will", "this", "that", "with", "from", "have", "been", "their",
    "they", "about", "would", "could", "which", "there", "before", "after",
    "than", "more", "some", "what", "when", "into", "also", "just", "does",
    "over", "most", "many", "only", "such", "very", "much", "then", "them",
    "each", "even", "back", "make", "like", "long", "come", "made", "find",
    "here", "know", "take", "want", "year", "first", "last", "next", "good",
    "give", "look", "help", "tell", "keep", "being", "still", "where", "thing",
    "every", "going", "under", "same", "right", "left", "think", "said", "says",
    "time", "people", "could", "state", "world", "other", "market", "markets",
    "price", "trade", "trading", "high", "higher", "lower", "latest",
    "news", "report", "according", "event", "events", "outcome",
    "between", "during", "should", "these", "those", "while", "since",
    "yes", "no",
})


# ═══════════════════════════════════════════════════════════════════════
# STEP 1: EXTRACT TOPICS FROM SUSPICIOUS TRADES
# ═══════════════════════════════════════════════════════════════════════

def _stem(word: str) -> str:
    """Crude suffix stripping so 'attacked' matches 'attack', etc."""
    w = word.lower()
    for suffix in ("tion", "sion", "ment", "ness", "ing", "ied", "ies",
                   "ated", "ting", "ted", "sed", "led", "ned", "red",
                   "ers", "ous", "ive", "ful", "ble", "ally", "ily",
                   "ed", "ly", "er", "es", "al", "'s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            return w[:-len(suffix)]
    if w.endswith("s") and len(w) >= 5:
        return w[:-1]
    return w


def extract_trade_topics(suspicious_trades: list[dict]) -> dict[str, list[dict]]:
    """Extract searchable topic keywords from each suspicious trade's market title.

    Returns: {keyword: [list of trades mentioning that keyword]}

    Uses stemming so "attacked" in a trade title can match "attack" or
    "airstrikes" won't match but "attack" will match "attacked"/"attacks".
    Also extracts named entities (proper nouns) which are the strongest signal.
    """
    topic_map: dict[str, list[dict]] = defaultdict(list)

    for trade in suspicious_trades:
        title = trade.get("title", "")

        # Extract meaningful words (3+ chars, not stop words)
        raw_words = set(re.findall(r'\b[a-zA-Z]{3,}\b', title.lower()))
        raw_words -= STOP_WORDS

        # Add both original and stemmed forms
        words = set()
        for w in raw_words:
            words.add(w)
            stemmed = _stem(w)
            if stemmed != w and len(stemmed) >= 3:
                words.add(stemmed)

        # Extract named entities: capitalized words (country names, people, orgs)
        entities = re.findall(r'\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})*\b', title)
        for ent in entities:
            key = ent.lower()
            if key not in STOP_WORDS and len(key) >= 3:
                words.add(key)

        for w in words:
            topic_map[w].append(trade)

    return dict(topic_map)


# ═══════════════════════════════════════════════════════════════════════
# STEP 2: FETCH BREAKING NEWS
# ═══════════════════════════════════════════════════════════════════════

def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _parse_rss_items(xml_content: bytes, source: str) -> list[dict]:
    """Parse RSS/Atom XML into article dicts."""
    articles = []
    try:
        root = ET.fromstring(xml_content)
        # RSS 2.0
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            desc = (item.findtext("description") or "").strip()
            if not title:
                continue
            articles.append({
                "title": title,
                "link": link,
                "source": source,
                "published": pub,
                "description": _strip_html(desc)[:500],
                "text_lower": f"{title} {_strip_html(desc)}".lower(),
            })
        # Atom fallback
        if not articles:
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall(".//atom:entry", ns):
                title = (entry.findtext("atom:title", namespaces=ns) or "").strip()
                link_el = entry.find("atom:link", ns)
                link = link_el.get("href", "") if link_el is not None else ""
                pub = (entry.findtext("atom:published", namespaces=ns)
                       or entry.findtext("atom:updated", namespaces=ns) or "").strip()
                desc = (entry.findtext("atom:summary", namespaces=ns) or "").strip()
                if not title:
                    continue
                articles.append({
                    "title": title,
                    "link": link,
                    "source": source,
                    "published": pub,
                    "description": _strip_html(desc)[:500],
                    "text_lower": f"{title} {_strip_html(desc)}".lower(),
                })
    except ET.ParseError:
        pass
    return articles


def fetch_breaking_news() -> list[dict]:
    """Fetch recent articles from all RSS feeds."""
    all_articles = []
    for url, source in NEWS_FEEDS:
        try:
            resp = requests.get(url, timeout=8, headers={"User-Agent": "CryptoEdge/1.0"})
            if resp.status_code == 200:
                all_articles.extend(_parse_rss_items(resp.content, source))
        except Exception:
            continue

    # Deduplicate by title
    seen = set()
    unique = []
    for a in all_articles:
        key = a["title"].lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(a)

    return unique


# ═══════════════════════════════════════════════════════════════════════
# STEP 3: CORRELATE TRADES WITH NEWS
# ═══════════════════════════════════════════════════════════════════════

def correlate_trades_with_news(
    suspicious_trades: list[dict],
    articles: list[dict],
) -> list[dict]:
    """Find news articles that validate suspicious trades.

    For each suspicious trade, check if any breaking news articles match
    the trade's market topic. Score the correlation based on:
    - Number of matching keywords (topic overlap)
    - Trade size and potential profit
    - Odds (but ALL odds levels, not just long shots)
    - Trade timing relative to news (if parseable)
    """
    # Build topic map
    topic_map = extract_trade_topics(suspicious_trades)
    if not topic_map:
        return []

    # For each article, check which trades it correlates with
    correlations = []
    seen_combos = set()  # (trade_key, article_title) to dedup

    # Pre-compute stemmed word sets for each article
    article_stems = []
    for article in articles:
        text = article["text_lower"]
        raw_words = set(re.findall(r'\b[a-z]{3,}\b', text))
        stemmed = {_stem(w) for w in raw_words if w not in STOP_WORDS}
        stemmed |= raw_words  # keep originals too
        article_stems.append(stemmed)

    for idx, article in enumerate(articles):
        text = article["text_lower"]
        stems = article_stems[idx]

        # Find all matching topics
        matched_trades: dict[str, dict] = {}  # trade_key -> match_info

        for keyword, trades in topic_map.items():
            # Check if keyword (or its stem) appears in article
            matched = False
            if keyword in stems:
                matched = True
            elif re.search(r'\b' + re.escape(keyword) + r'\b', text):
                matched = True

            if matched:
                for t in trades:
                    tk = _trade_key(t)
                    if tk not in matched_trades:
                        matched_trades[tk] = {"trade": t, "keywords": [], "phrase_matches": 0}
                    matched_trades[tk]["keywords"].append(keyword)

        # Score each trade-article correlation
        for tk, info in matched_trades.items():
            trade = info["trade"]
            keywords = list(set(info["keywords"]))  # dedup stems/originals
            phrase_matches = info["phrase_matches"]

            combo_key = f"{tk}_{article['title'][:30]}"
            if combo_key in seen_combos:
                continue
            seen_combos.add(combo_key)

            # Need at least 1 keyword match, but score adjusts for strength
            if not keywords:
                continue

            score = _score_correlation(trade, article, keywords, phrase_matches)
            if score < 15:
                continue

            alert_id = hashlib.sha256(
                f"{tk}_{article['title']}".encode()
            ).hexdigest()[:16]

            correlations.append({
                "id": alert_id,
                # News info
                "title": article["title"],
                "link": article.get("link", ""),
                "source": article.get("source", ""),
                "published": article.get("published", ""),
                "description": article.get("description", ""),
                # Trade info
                "trade_title": trade.get("title", ""),
                "trade_market_id": trade.get("market_id", ""),
                "trade_outcome": trade.get("outcome", ""),
                "trade_size": trade.get("usd_value", 0),
                "trade_odds": trade.get("price", 0),
                "trade_odds_str": trade.get("odds_str", ""),
                "trade_potential_profit": trade.get("potential_profit", 0),
                "trade_score": trade.get("score", 0),
                "trade_wallet": trade.get("wallet", "")[:12],
                "trade_time": trade.get("time_str", ""),
                # Correlation info
                "matched_keywords": keywords[:10],
                "score": score,
                "reasons": _build_reasons(trade, article, keywords, phrase_matches, score),
                "scanned_at": datetime.now(timezone.utc).isoformat(),
                # For DB compat
                "insider_keywords": keywords[:10],
                "event_keywords": [],
                "amounts": [],
                "related_markets": [{
                    "market_question": trade.get("title", ""),
                    "slug": trade.get("market_id", ""),
                    "volume_24h": trade.get("market_volume", 0),
                    "liquidity": 0,
                    "match_score": len(keywords) * 10,
                    "matched_words": keywords[:5],
                }],
            })

    # Sort by score
    correlations.sort(key=lambda x: x["score"], reverse=True)
    return correlations


def _trade_key(trade: dict) -> str:
    """Unique key for a trade."""
    return f"{trade.get('wallet', '')[:12]}_{trade.get('usd_value', 0):.0f}_{trade.get('title', '')[:20]}"


def _score_correlation(trade: dict, article: dict, keywords: list, phrase_matches: int) -> int:
    """Score how suspicious a trade-news correlation is."""
    score = 0

    # 1. Keyword overlap strength (more matches = stronger correlation)
    n_keywords = len(keywords)
    if n_keywords >= 5:
        score += 35
    elif n_keywords >= 4:
        score += 28
    elif n_keywords >= 3:
        score += 20
    elif n_keywords >= 2:
        score += 12

    # Phrase matches are much stronger than single-word matches
    score += phrase_matches * 8

    # 2. Trade size — bigger bets are more suspicious when correlated with news
    usd = trade.get("usd_value", 0)
    if usd >= 100000:
        score += 30
    elif usd >= 50000:
        score += 22
    elif usd >= 20000:
        score += 15
    elif usd >= 10000:
        score += 10
    elif usd >= 5000:
        score += 5

    # 3. Potential profit — the real payoff
    profit = trade.get("potential_profit", 0)
    if profit >= 100000:
        score += 25
    elif profit >= 50000:
        score += 18
    elif profit >= 20000:
        score += 12
    elif profit >= 10000:
        score += 8
    elif profit >= 5000:
        score += 4

    # 4. Odds context — ALL odds levels, not just long shots
    #    Medium odds with a huge bet + news correlation is VERY suspicious
    price = trade.get("price", 0.5)
    if price <= 0.05:
        score += 15  # extreme long shot — very suspicious
    elif price <= 0.15:
        score += 12  # long shot
    elif price <= 0.30:
        score += 8   # underdog — still suspicious with news correlation
    elif price <= 0.50:
        score += 5   # medium odds — suspicious if large + news confirms
    elif price <= 0.70:
        score += 3   # slight favorite — still flag if correlated
    # Even 70%+ odds: if someone drops $200K right before news, it's notable

    # 5. Original suspicion score from the trade scanner
    trade_score = trade.get("score", 0)
    if trade_score >= 50:
        score += 15
    elif trade_score >= 30:
        score += 8
    elif trade_score >= 15:
        score += 3

    return min(score, 100)


def _build_reasons(trade: dict, article: dict, keywords: list, phrase_matches: int, score: int) -> list[str]:
    """Build human-readable reasons for the correlation."""
    reasons = []

    usd = trade.get("usd_value", 0)
    profit = trade.get("potential_profit", 0)
    price = trade.get("price", 0)
    odds_str = trade.get("odds_str", f"{price:.0%}")

    reasons.append(
        f"${usd:,.0f} bet at {odds_str} odds → ${profit:,.0f} potential profit"
    )

    if phrase_matches > 0:
        reasons.append(f"{phrase_matches} phrase match(es) between trade market and news headline")

    reasons.append(f"{len(keywords)} keyword overlaps: {', '.join(keywords[:5])}")

    reasons.append(
        f"News: \"{article['title'][:60]}\" ({article.get('source', 'Unknown')})"
    )

    if trade.get("time_str"):
        reasons.append(f"Trade placed: {trade['time_str']}")

    if price <= 0.15:
        reasons.append(f"Long-shot bet ({odds_str}) — high insider signal")
    elif price <= 0.35:
        reasons.append(f"Underdog bet ({odds_str}) — moderate insider signal")
    elif price <= 0.55:
        reasons.append(f"Medium-odds bet ({odds_str}) — notable with news confirmation")

    return reasons


# ═══════════════════════════════════════════════════════════════════════
# MAIN SCANNER
# ═══════════════════════════════════════════════════════════════════════

def run_news_trade_scan(suspicious_trades: list[dict] | None = None) -> dict:
    """Run a full news-trade correlation scan.

    Args:
        suspicious_trades: Pre-computed suspicious trades from the main scanner.
            If None, runs the scanner fresh.

    Returns dict with:
        alerts: list of trade-news correlation alerts
        scan_time: ISO timestamp
        articles_scanned: total articles checked
        trades_checked: number of suspicious trades checked
        alerts_found: number of correlations found
    """
    print("[NEWS-TRADE] Starting correlation scan...")
    start = time.time()

    # 1. Get suspicious trades (from cache or fresh scan)
    if suspicious_trades is None:
        try:
            from suspicious_trades import run_scanner
            result = run_scanner()
            suspicious_trades = result.get("suspicious_trades", []) if result else []
        except Exception as e:
            print(f"[NEWS-TRADE] Failed to get suspicious trades: {e}")
            suspicious_trades = []

    if not suspicious_trades:
        print("[NEWS-TRADE] No suspicious trades to correlate.")
        return {
            "alerts": [],
            "scan_time": datetime.now(timezone.utc).isoformat(),
            "articles_scanned": 0,
            "trades_checked": 0,
            "alerts_found": 0,
        }

    print(f"[NEWS-TRADE] Checking {len(suspicious_trades)} suspicious trades against breaking news...")

    # 2. Fetch breaking news
    articles = fetch_breaking_news()
    print(f"[NEWS-TRADE] Fetched {len(articles)} unique articles from {len(NEWS_FEEDS)} feeds")

    # 3. Correlate
    alerts = correlate_trades_with_news(suspicious_trades, articles)

    elapsed = time.time() - start
    print(f"[NEWS-TRADE] Scan complete: {len(alerts)} correlations from "
          f"{len(suspicious_trades)} trades × {len(articles)} articles ({elapsed:.1f}s)")

    if alerts:
        top = alerts[0]
        print(f"[NEWS-TRADE] Top alert [{top['score']}/100]: "
              f"${top['trade_size']:,.0f} bet on \"{top['trade_title'][:40]}\" "
              f"↔ \"{top['title'][:40]}\"")

    # Cache
    result = {
        "alerts": alerts,
        "scan_time": datetime.now(timezone.utc).isoformat(),
        "articles_scanned": len(articles),
        "trades_checked": len(suspicious_trades),
        "alerts_found": len(alerts),
    }
    try:
        cache_file = CACHE_DIR / f"news_trade_{datetime.now(timezone.utc).strftime('%Y%m%d_%H')}.json"
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(cache_file), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(result, f, indent=2, default=str)
            os.replace(tmp, cache_file)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:
        pass

    return result

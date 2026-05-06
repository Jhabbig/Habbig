#!/usr/bin/env python3
"""World State Dashboard — FastAPI backend."""

import asyncio
import hmac
import json
import logging
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request
try:
    from defusedxml.ElementTree import fromstring as _xml_fromstring
except ImportError:
    raise ImportError("defusedxml is required: pip install defusedxml")
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from infrastructure_data import (
    UNDERSEA_CABLES as _INFRA_CABLES,
    OIL_GAS_PIPELINES as _INFRA_PIPELINES,
    OIL_RARE_EARTH_FIELDS as _INFRA_FIELDS,
)
import cross_dashboard
import analyst_db
import event_extractor

analyst_db.init_db()

app = FastAPI(title="World State Dashboard")

HTML_PATH = Path(__file__).parent / "index.html"
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_sso_secret = os.environ.get("GATEWAY_SSO_SECRET", "")
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"
if not _sso_secret:
    if _DEV_MODE:
        logging.warning("GATEWAY_SSO_SECRET not set — world-state dashboard running in DEV_MODE (no auth)")
    else:
        logging.warning("GATEWAY_SSO_SECRET not set and DEV_MODE not enabled — rejecting all requests")


@app.middleware("http")
async def security_and_auth(request: Request, call_next):
    # Authenticate via gateway SSO header
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
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'; frame-ancestors 'none'"
    if _sso_secret:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


# ── Thread safety for global caches ──────────────────────────────────────────
_cache_lock = threading.Lock()

# ── News Feed (RSS aggregation) ───────────────────────────────────────────────
NEWS_CACHE = {"data": [], "fetched_at": 0.0}
NEWS_CACHE_TTL = 90  # 90 seconds

NEWS_FEEDS = [
    {"name": "BBC World", "url": "http://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"name": "Guardian", "url": "https://www.theguardian.com/world/rss"},
    {"name": "NPR World", "url": "https://feeds.npr.org/1004/rss.xml"},
    {"name": "DW World", "url": "https://rss.dw.com/rdf/rss-en-world"},
    {"name": "France24", "url": "https://www.france24.com/en/rss"},
]


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_date(pub_date: str):
    try:
        dt = parsedate_to_datetime(pub_date)
        # Ensure timezone-aware for consistent sorting
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def fetch_news():
    now = time.time()
    with _cache_lock:
        if NEWS_CACHE["data"] and (now - NEWS_CACHE["fetched_at"]) < NEWS_CACHE_TTL:
            return NEWS_CACHE["data"]

    all_items = []
    for feed in NEWS_FEEDS:
        try:
            req = urllib.request.Request(
                feed["url"],
                headers={"User-Agent": "Mozilla/5.0 (WorldMonitor/1.0)"},
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                xml_data = resp.read()
            root = _xml_fromstring(xml_data)

            # Handle both RSS 2.0 and RDF/Atom-ish feeds
            items = root.findall(".//item")
            if not items:
                # Atom feed
                ns = {"a": "http://www.w3.org/2005/Atom"}
                items = root.findall("a:entry", ns)

            for item in items[:12]:
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub_date = (item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date") or "").strip()
                desc = _strip_html(item.findtext("description") or "")[:180]

                if not title:
                    continue
                # Validate link scheme to prevent javascript: URI injection
                if link and not link.startswith(("http://", "https://")):
                    link = ""

                all_items.append({
                    "source": feed["name"],
                    "title": title,
                    "link": link,
                    "pub_date": pub_date,
                    "description": desc,
                })
        except Exception as e:
            print(f"[news] {feed['name']} failed: {e}")

    all_items.sort(key=lambda x: _parse_date(x["pub_date"]), reverse=True)
    with _cache_lock:
        NEWS_CACHE["data"] = all_items[:60]
        NEWS_CACHE["fetched_at"] = now

    # Extract typed events into the analyst DB. Cheap (regex-based) so we run
    # it inline; failures here must never break the news endpoint.
    try:
        _ingest_events_from_news(all_items[:60])
    except Exception as e:
        logging.warning("event ingestion failed: %s", e)

    return NEWS_CACHE["data"]


def _ingest_events_from_news(items):
    candidates = event_extractor.extract_batch(items)
    for ev in candidates:
        src = ev["source"]
        try:
            source_id = analyst_db.upsert_source(
                publisher=src["publisher"], title=src["title"], url=src["url"],
                snippet=src["snippet"], published_at=src["published_at"],
            )
            analyst_db.insert_event(
                event_type=ev["type"], summary=ev["summary"],
                occurred_at=ev["occurred_at"], lat=ev["lat"], lon=ev["lon"],
                confidence=ev["confidence"], severity=ev["severity"],
                actors=ev["actors"], source_ids=[source_id],
            )
        except Exception as e:
            logging.warning("event persist failed: %s", e)


# ── Polymarket (prediction markets) ──────────────────────────────────────────
POLYMARKET_CACHE = {"data": [], "fetched_at": 0.0}
POLYMARKET_CACHE_TTL = 60  # 60 seconds

# Keywords for "politically/geopolitically relevant" filtering when category is missing
POLY_POL_KEYWORDS = [
    "election", "president", "war", "nuclear", "nato", "ukraine", "russia", "china", "taiwan",
    "israel", "iran", "gaza", "hezbollah", "hamas", "putin", "xi ", "trump", "biden", "harris",
    "vance", "netanyahu", "zelensky", "kim jong", "macron", "merz", "geopolitic", "missile",
    "drone", "sanctions", "cease", "coup", "invasion", "fed ", "rate cut", "inflation", "recession",
    "senate", "congress", "supreme court", "ruling", "eu ", "brexit", "parliament", "cabinet",
    "minister", "ambassador", "summit", "treaty", "border", "mexico", "venezuela", "saudi",
    "pakistan", "india", "north korea", "south korea", "japan", "syria", "sudan", "yemen",
    "polymarket", "kalshi",
]


def _poly_is_political(market: dict) -> bool:
    cat = (market.get("category") or "").lower()
    if any(k in cat for k in ["politic", "geopolit", "election", "world", "news", "war", "international"]):
        return True
    q = (market.get("question") or "").lower()
    return any(k in q for k in POLY_POL_KEYWORDS)


def fetch_polymarket():
    now = time.time()
    with _cache_lock:
        if POLYMARKET_CACHE["data"] and (now - POLYMARKET_CACHE["fetched_at"]) < POLYMARKET_CACHE_TTL:
            return POLYMARKET_CACHE["data"]

    results = []
    try:
        # Sort by 24h volume to get what's trading right now
        url = "https://gamma-api.polymarket.com/markets?closed=false&active=true&limit=100&order=volume24hr&ascending=false"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (WorldMonitor/1.0)"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read()
        markets = json.loads(raw)
        if not isinstance(markets, list):
            markets = []

        for m in markets:
            try:
                outcomes_raw = m.get("outcomes") or "[]"
                prices_raw = m.get("outcomePrices") or "[]"
                outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw

                if not outcomes or not prices:
                    continue

                # Find top (highest-priced) outcome
                float_prices = []
                for p in prices:
                    try:
                        float_prices.append(float(p))
                    except Exception:
                        float_prices.append(0.0)

                if float_prices:
                    top_idx = float_prices.index(max(float_prices))
                    top_price = float_prices[top_idx]
                else:
                    top_idx = 0
                    top_price = 0.0
                if outcomes and 0 <= top_idx < len(outcomes):
                    top_outcome = outcomes[top_idx]
                else:
                    top_outcome = "Yes"

                vol_24h = m.get("volume24hr") or m.get("volumeNum") or m.get("volume") or 0
                try:
                    vol_24h = float(vol_24h)
                except Exception:
                    vol_24h = 0.0

                vol_total = m.get("volumeNum") or m.get("volume") or 0
                try:
                    vol_total = float(vol_total)
                except Exception:
                    vol_total = 0.0

                item = {
                    "id": m.get("id"),
                    "question": m.get("question") or "",
                    "slug": m.get("slug") or "",
                    "category": m.get("category") or "",
                    "end_date": m.get("endDate") or "",
                    "top_outcome": top_outcome,
                    "top_price": top_price,
                    "outcomes": outcomes,
                    "prices": float_prices,
                    "volume_24h": vol_24h,
                    "volume_total": vol_total,
                    "liquidity": float(m.get("liquidityNum") or m.get("liquidity") or 0),
                    "icon": m.get("icon") or m.get("image") or "",
                }
                if _poly_is_political(m):
                    results.append(item)
            except Exception as e:
                print(f"[polymarket] parse error: {e}")
                continue

        # Sort by 24h volume desc, keep top 25
        results.sort(key=lambda x: x["volume_24h"], reverse=True)
        results = results[:25]
    except Exception as e:
        print(f"[polymarket] fetch failed: {e}")

    with _cache_lock:
        POLYMARKET_CACHE["data"] = results
        POLYMARKET_CACHE["fetched_at"] = now
    return results


# ── X / Twitter Intelligence Feed ────────────────────────────────────────────
XFEED_CACHE = {"data": [], "fetched_at": 0.0}
XFEED_CACHE_TTL = 300  # 5 minutes — X API rate limits are strict

X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "")

# Key geopolitical figures and institutions to track
X_ACCOUNTS = [
    # ── World Leaders ──
    {"handle": "POTUS", "name": "President of the United States", "category": "leader", "country": "US"},
    {"handle": "realDonaldTrump", "name": "Donald Trump", "category": "leader", "country": "US"},
    {"handle": "VP", "name": "Vice President of the United States", "category": "leader", "country": "US"},
    {"handle": "ZelenskyyUa", "name": "Volodymyr Zelenskyy", "category": "leader", "country": "UA"},
    {"handle": "EmmanuelMacron", "name": "Emmanuel Macron", "category": "leader", "country": "FR"},
    {"handle": "OlafScholz", "name": "Olaf Scholz", "category": "leader", "country": "DE"},
    {"handle": "RishiSunak", "name": "Rishi Sunak", "category": "leader", "country": "GB"},
    {"handle": "KeirStarmer", "name": "Keir Starmer", "category": "leader", "country": "GB"},
    {"handle": "naaboripamo", "name": "Narendra Modi", "category": "leader", "country": "IN"},
    {"handle": "IsraeliPM", "name": "Prime Minister of Israel", "category": "leader", "country": "IL"},
    {"handle": "RTErdogan", "name": "Recep Tayyip Erdogan", "category": "leader", "country": "TR"},
    {"handle": "Tsaborni", "name": "Giorgia Meloni", "category": "leader", "country": "IT"},
    {"handle": "presidaboric", "name": "Gabriel Boric", "category": "leader", "country": "CL"},
    # ── Institutions ──
    {"handle": "NATO", "name": "NATO", "category": "institution", "country": "INT"},
    {"handle": "UN", "name": "United Nations", "category": "institution", "country": "INT"},
    {"handle": "EU_Commission", "name": "European Commission", "category": "institution", "country": "EU"},
    {"handle": "StateDept", "name": "US State Department", "category": "institution", "country": "US"},
    {"handle": "DeptofDefense", "name": "US Department of Defense", "category": "institution", "country": "US"},
    {"handle": "SecDef", "name": "US Secretary of Defense", "category": "institution", "country": "US"},
    {"handle": "CIA", "name": "CIA", "category": "institution", "country": "US"},
    {"handle": "ABORNI", "name": "IAEA", "category": "institution", "country": "INT"},
    {"handle": "WHO", "name": "World Health Organization", "category": "institution", "country": "INT"},
    {"handle": "WFP", "name": "World Food Programme", "category": "institution", "country": "INT"},
    # ── Defense & Intel Analysts ──
    {"handle": "KofmanMichael", "name": "Michael Kofman", "category": "analyst", "country": "US"},
    {"handle": "RALee85", "name": "Rob Lee", "category": "analyst", "country": "US"},
    {"handle": "DefMon3", "name": "DefMon", "category": "analyst", "country": "INT"},
    {"handle": "wartranslated", "name": "WarTranslated", "category": "analyst", "country": "INT"},
    {"handle": "NOELreports", "name": "NOEL Reports", "category": "analyst", "country": "INT"},
    {"handle": "sentdefender", "name": "OSINTdefender", "category": "analyst", "country": "INT"},
    # ── Key Journalists ──
    {"handle": "christaborni", "name": "Christiane Amanpour", "category": "journalist", "country": "US"},
    {"handle": "baborni", "name": "BBC Breaking News", "category": "journalist", "country": "GB"},
    {"handle": "Reuters", "name": "Reuters", "category": "journalist", "country": "INT"},
    {"handle": "AP", "name": "Associated Press", "category": "journalist", "country": "INT"},
]

# ── Sentiment & Analysis Keywords ──
_NEGATIVE_WORDS = frozenset([
    "war", "attack", "strike", "bomb", "missile", "kill", "dead", "death", "casualt",
    "threat", "danger", "crisis", "conflict", "invade", "invasion", "escalat", "tension",
    "sanction", "collapse", "destroy", "explosion", "terror", "nuclear", "weapon", "shoot",
    "hostage", "siege", "retreat", "defeat", "violated", "breach", "warning", "urgent",
    "catastroph", "disaster", "famine", "refugee", "displac", "evacuate", "emergency",
    "coup", "assassination", "detained", "arrest", "condemn", "provocat", "retaliat",
])

_POSITIVE_WORDS = frozenset([
    "peace", "ceasefire", "agreement", "treaty", "diplomacy", "negotiat", "cooperat",
    "aid", "humanitarian", "relief", "rebuild", "stabiliz", "de-escalat", "dialogue",
    "alliance", "partner", "support", "progress", "reform", "liberat", "protect",
    "reunif", "reconcil", "elected", "democratic", "freedom", "resolution", "accord",
])

_TOPIC_KEYWORDS = {
    "conflict": ["war", "attack", "strike", "bomb", "missile", "battle", "offensive", "front", "casualt", "combat", "troops", "military"],
    "diplomacy": ["negotiate", "summit", "treaty", "agreement", "diplomat", "ambassador", "dialogue", "talks", "ceasefire", "peace", "accord"],
    "nuclear": ["nuclear", "warhead", "enrichment", "uranium", "plutonium", "IAEA", "nonprolif", "atomic", "radiation"],
    "sanctions": ["sanction", "embargo", "restrict", "ban", "tariff", "trade war", "frozen assets", "blacklist"],
    "intelligence": ["intelligence", "espionage", "spy", "surveillance", "cyber", "hack", "intercept", "classified"],
    "military": ["deploy", "naval", "aircraft carrier", "submarine", "fighter jet", "drone", "regiment", "battalion", "exercise", "NATO", "defense"],
    "humanitarian": ["refugee", "displaced", "famine", "aid", "humanitarian", "crisis", "evacuat", "food", "shelter", "UNHCR", "WFP"],
    "election": ["election", "vote", "ballot", "poll", "candidat", "campaign", "democrat", "inaugurat", "referendum"],
    "economy": ["economy", "GDP", "inflation", "recession", "trade", "market", "debt", "fiscal", "monetary", "currency", "oil price"],
    "climate": ["climate", "emission", "carbon", "warming", "flood", "drought", "wildfire", "hurricane", "typhoon", "disaster"],
}

_URGENCY_KEYWORDS = frozenset([
    "breaking", "urgent", "just in", "developing", "alert", "imminent",
    "emergency", "critical", "live", "happening now", "confirmed",
])

_COUNTRY_MENTIONS = {
    "US": ["united states", "america", "washington", "pentagon", "white house", "congress", "biden", "trump"],
    "RU": ["russia", "moscow", "kremlin", "putin"],
    "UA": ["ukraine", "kyiv", "kiev", "zelenskyy", "zelensky"],
    "CN": ["china", "beijing", "xi jinping", "taiwan strait", "south china sea"],
    "IL": ["israel", "jerusalem", "tel aviv", "netanyahu", "idf", "gaza"],
    "IR": ["iran", "tehran", "khamenei", "irgc", "hezbollah"],
    "KP": ["north korea", "pyongyang", "kim jong"],
    "TW": ["taiwan", "taipei"],
    "SY": ["syria", "damascus", "assad"],
    "YE": ["yemen", "houthi", "sanaa"],
    "SD": ["sudan", "khartoum", "rsf", "rapid support"],
    "MM": ["myanmar", "burma", "junta"],
    "PK": ["pakistan", "islamabad"],
    "IN": ["india", "delhi", "modi"],
    "SA": ["saudi", "riyadh", "mbs"],
    "TR": ["turkey", "türkiye", "ankara", "erdogan"],
    "DE": ["germany", "berlin", "scholz", "merz"],
    "FR": ["france", "paris", "macron"],
    "GB": ["britain", "london", "uk", "starmer"],
}


def _analyze_post(text: str) -> dict:
    """Analyze a tweet for sentiment, topics, urgency, and mentioned countries."""
    lower = text.lower()

    # Sentiment
    neg_count = sum(1 for w in _NEGATIVE_WORDS if w in lower)
    pos_count = sum(1 for w in _POSITIVE_WORDS if w in lower)
    if neg_count > pos_count + 1:
        sentiment = "negative"
    elif pos_count > neg_count + 1:
        sentiment = "positive"
    else:
        sentiment = "neutral"

    # Topics
    topics = []
    for topic, keywords in _TOPIC_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            topics.append(topic)
    if not topics:
        topics = ["general"]

    # Urgency
    is_urgent = any(kw in lower for kw in _URGENCY_KEYWORDS)

    # Country mentions
    countries = []
    for code, keywords in _COUNTRY_MENTIONS.items():
        if any(kw in lower for kw in keywords):
            countries.append(code)

    return {
        "sentiment": sentiment,
        "topics": topics[:3],
        "urgent": is_urgent,
        "countries": countries[:5],
    }


def _x_api_request(url: str) -> dict:
    """Make an authenticated request to the X API v2."""
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {X_BEARER_TOKEN}",
        "User-Agent": "WorldMonitor/1.0",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _resolve_user_ids(handles: list[str]) -> dict:
    """Resolve X handles to user IDs (batch, max 100)."""
    usernames = ",".join(handles[:100])
    url = f"https://api.x.com/2/users/by?usernames={usernames}&user.fields=id,name,username,profile_image_url,public_metrics"
    data = _x_api_request(url)
    result = {}
    for user in data.get("data", []):
        result[user["username"].lower()] = user
    return result


# Persistent user ID cache (resolved once, reused)
_USER_ID_CACHE = {"ids": {}, "resolved": False}


def fetch_xfeed():
    """Fetch recent posts from key geopolitical figures on X."""
    now = time.time()
    with _cache_lock:
        if XFEED_CACHE["data"] and (now - XFEED_CACHE["fetched_at"]) < XFEED_CACHE_TTL:
            return XFEED_CACHE["data"]

    if not X_BEARER_TOKEN:
        print("[xfeed] No X_BEARER_TOKEN set — skipping X feed")
        return []

    results = []

    try:
        # Resolve user IDs if needed
        if not _USER_ID_CACHE["resolved"]:
            handles = [a["handle"] for a in X_ACCOUNTS]
            all_resolved = True
            # Batch in groups of 100
            for i in range(0, len(handles), 100):
                batch = handles[i:i+100]
                try:
                    resolved = _resolve_user_ids(batch)
                    _USER_ID_CACHE["ids"].update(resolved)
                except Exception as e:
                    print(f"[xfeed] user resolve batch {i} failed: {e}")
                    all_resolved = False
            if all_resolved:
                _USER_ID_CACHE["resolved"] = True

        # Build handle→account lookup
        account_lookup = {a["handle"].lower(): a for a in X_ACCOUNTS}

        # Fetch recent tweets from each user (limit API calls — use search instead)
        # Strategy: use search endpoint with OR query for all handles
        # This is more efficient than per-user timeline calls
        handle_chunks = []
        chunk = []
        for a in X_ACCOUNTS:
            uid_info = _USER_ID_CACHE["ids"].get(a["handle"].lower())
            if uid_info:
                chunk.append(f"from:{a['handle']}")
                if len(chunk) >= 15:  # X search OR limit
                    handle_chunks.append(chunk)
                    chunk = []
        if chunk:
            handle_chunks.append(chunk)

        for chunk in handle_chunks[:3]:  # Limit to 3 search calls
            query = " OR ".join(chunk)
            url = (
                f"https://api.x.com/2/tweets/search/recent"
                f"?query={urllib.parse.quote(query)}"
                f"&max_results=50"
                f"&tweet.fields=created_at,public_metrics,author_id,lang"
                f"&expansions=author_id"
                f"&user.fields=username,name,profile_image_url"
            )
            try:
                resp_data = _x_api_request(url)

                # Build author lookup from includes
                authors = {}
                for u in resp_data.get("includes", {}).get("users", []):
                    authors[u["id"]] = u

                for tweet in resp_data.get("data", []):
                    author_id = tweet.get("author_id", "")
                    author = authors.get(author_id, {})
                    username = author.get("username", "unknown").lower()
                    acct = account_lookup.get(username, {})
                    metrics = tweet.get("public_metrics", {})

                    # Skip non-English or very short tweets
                    text = tweet.get("text", "")
                    if len(text) < 20:
                        continue

                    analysis = _analyze_post(text)

                    # Impact score: combination of engagement + urgency + account importance
                    engagement = (
                        metrics.get("like_count", 0)
                        + metrics.get("retweet_count", 0) * 3
                        + metrics.get("reply_count", 0) * 2
                        + metrics.get("quote_count", 0) * 2
                    )
                    cat_weight = {"leader": 3, "institution": 2, "analyst": 1.5, "journalist": 1}.get(acct.get("category", ""), 1)
                    urgency_bonus = 1.5 if analysis["urgent"] else 1.0
                    impact_score = round(min(10, (math.log10(max(engagement, 1)) * cat_weight * urgency_bonus)), 1)

                    results.append({
                        "id": tweet["id"],
                        "text": text,
                        "author_handle": author.get("username", "unknown"),
                        "author_name": acct.get("name") or author.get("name", "Unknown"),
                        "author_category": acct.get("category", "unknown"),
                        "author_country": acct.get("country", "INT"),
                        "author_avatar": author.get("profile_image_url", ""),
                        "created_at": tweet.get("created_at", ""),
                        "likes": metrics.get("like_count", 0),
                        "retweets": metrics.get("retweet_count", 0),
                        "replies": metrics.get("reply_count", 0),
                        "quotes": metrics.get("quote_count", 0),
                        "url": f"https://x.com/{author.get('username', 'x')}/status/{tweet['id']}",
                        "analysis": analysis,
                        "impact_score": impact_score,
                    })
            except Exception as e:
                print(f"[xfeed] search chunk failed: {e}")

        # Sort by impact score desc, then by recency
        results.sort(key=lambda x: (x["impact_score"], x["created_at"]), reverse=True)
        results = results[:80]

    except Exception as e:
        print(f"[xfeed] fetch failed: {e}")

    with _cache_lock:
        XFEED_CACHE["data"] = results
        XFEED_CACHE["fetched_at"] = now
    return results


# ── Conflict Data ────────────────────────────────────────────────────────────
CONFLICTS = [
    {
        "id": "russia-ukraine",
        "name": "Russia — Ukraine",
        "type": "interstate_war",
        "status": "Active",
        "started": "2022-02-24",
        "parties": ["Russia", "Ukraine (+ Western support)"],
        "casualties": "500 000+",
        "description": "Full-scale Russian invasion of Ukraine. Largest land war in Europe since WWII.",
        "startLat": 55.75, "startLng": 37.62,
        "endLat": 50.45, "endLng": 30.52,
        "intensity": 5,
    },
    {
        "id": "israel-gaza",
        "name": "Israel — Gaza / Hamas",
        "type": "asymmetric_war",
        "status": "Active",
        "started": "2023-10-07",
        "parties": ["Israel", "Hamas / Palestinian groups"],
        "casualties": "150 000+",
        "description": "Israeli military operation in Gaza following October 7 attacks.",
        "startLat": 31.77, "startLng": 35.21,
        "endLat": 31.42, "endLng": 34.35,
        "intensity": 5,
    },
    {
        "id": "sudan-civil-war",
        "name": "Sudan Civil War",
        "type": "civil_war",
        "status": "Active",
        "started": "2023-04-15",
        "parties": ["Sudanese Armed Forces (SAF)", "Rapid Support Forces (RSF)"],
        "casualties": "150 000+",
        "description": "Civil war between SAF and RSF. Massive humanitarian crisis with millions displaced.",
        "startLat": 15.59, "startLng": 32.53,
        "endLat": 13.18, "endLng": 30.22,
        "intensity": 5,
    },
    {
        "id": "myanmar-civil-war",
        "name": "Myanmar Civil War",
        "type": "civil_war",
        "status": "Active",
        "started": "2021-02-01",
        "parties": ["Military Junta (Tatmadaw)", "Resistance forces (NUG, PDFs, ethnic armies)"],
        "casualties": "50 000+",
        "description": "Armed resistance following 2021 coup. Junta losing territory to resistance forces.",
        "startLat": 19.76, "startLng": 96.07,
        "endLat": 21.97, "endLng": 96.08,
        "intensity": 4,
    },
    {
        "id": "ethiopia-regional",
        "name": "Ethiopia — Regional Conflicts",
        "type": "civil_conflict",
        "status": "Active",
        "started": "2020",
        "parties": ["Ethiopian govt", "Amhara Fano militia", "OLA"],
        "casualties": "600 000+ (incl. Tigray war)",
        "description": "Post-Tigray war instability. Ongoing fighting in Amhara and Oromia regions.",
        "startLat": 9.02, "startLng": 38.75,
        "endLat": 11.59, "endLng": 37.39,
        "intensity": 3,
    },
    {
        "id": "drc-m23",
        "name": "DRC — M23 / Rwanda",
        "type": "proxy_war",
        "status": "Active",
        "started": "2022",
        "parties": ["DRC govt + allied militias", "M23 (Rwanda-backed)"],
        "casualties": "10 000+",
        "description": "M23 rebel advance in eastern DRC, backed by Rwanda. UN peacekeepers withdrawing.",
        "startLat": -1.68, "startLng": 29.22,
        "endLat": -1.94, "endLng": 29.87,
        "intensity": 4,
    },
    {
        "id": "yemen-houthis",
        "name": "Yemen — Houthi Conflict",
        "type": "asymmetric_war",
        "status": "Active",
        "started": "2014",
        "parties": ["Houthis (Ansar Allah)", "Saudi coalition / US-UK (Red Sea ops)"],
        "casualties": "150 000+",
        "description": "Houthi Red Sea shipping attacks triggered US/UK military strikes. Internal war ongoing.",
        "startLat": 15.37, "startLng": 44.19,
        "endLat": 13.00, "endLng": 43.00,
        "intensity": 4,
    },
    {
        "id": "syria-ongoing",
        "name": "Syria — Post-Assad Instability",
        "type": "civil_conflict",
        "status": "Active",
        "started": "2011",
        "parties": ["HTS-led govt", "SDF / Kurdish forces", "ISIS remnants", "Turkey-backed groups"],
        "casualties": "500 000+ (total war)",
        "description": "Post-Assad transition. Multiple armed factions, Turkish operations in the north, ISIS insurgency.",
        "startLat": 33.51, "startLng": 36.29,
        "endLat": 36.20, "endLng": 37.16,
        "intensity": 3,
    },
    {
        "id": "sahel-jihadist",
        "name": "Sahel — Jihadist Insurgency",
        "type": "insurgency",
        "status": "Active",
        "started": "2012",
        "parties": ["Mali / Burkina Faso / Niger juntas", "JNIM (al-Qaeda)", "ISGS (ISIS)"],
        "casualties": "50 000+",
        "description": "Jihadist insurgency across the Sahel. Military juntas expelled French forces, allied with Russia.",
        "startLat": 12.64, "startLng": -8.00,
        "endLat": 14.09, "endLng": 0.80,
        "intensity": 4,
    },
    {
        "id": "somalia-alshabaab",
        "name": "Somalia — Al-Shabaab",
        "type": "insurgency",
        "status": "Active",
        "started": "2006",
        "parties": ["Somali govt + AU forces", "Al-Shabaab"],
        "casualties": "20 000+",
        "description": "Long-running Islamist insurgency. Al-Shabaab controls large rural areas.",
        "startLat": 2.05, "startLng": 45.34,
        "endLat": 1.00, "endLng": 44.00,
        "intensity": 3,
    },
    {
        "id": "haiti-gangs",
        "name": "Haiti — Gang Crisis",
        "type": "state_collapse",
        "status": "Active",
        "started": "2021",
        "parties": ["Armed gangs (Viv Ansanm coalition)", "Transitional govt / Kenyan-led MSS"],
        "casualties": "10 000+",
        "description": "Armed gangs control most of Port-au-Prince. State effectively collapsed.",
        "startLat": 18.54, "startLng": -72.34,
        "endLat": 18.50, "endLng": -72.30,
        "intensity": 3,
    },
    {
        "id": "pakistan-balochistan",
        "name": "Pakistan — Balochistan / TTP",
        "type": "insurgency",
        "status": "Active",
        "started": "2004",
        "parties": ["Pakistani military", "TTP / BLA / BLF"],
        "casualties": "80 000+",
        "description": "Twin insurgencies: Islamist TTP and Baloch separatists. Escalating attacks on military and Chinese targets.",
        "startLat": 33.69, "startLng": 73.04,
        "endLat": 30.20, "endLng": 67.00,
        "intensity": 3,
    },
]

# Hotspot / tension zones (not full wars but elevated risk)
HOTSPOTS = [
    {"name": "Taiwan Strait", "lat": 23.70, "lng": 120.96, "level": "high", "note": "Chinese military pressure, US arms sales"},
    {"name": "Korean Peninsula", "lat": 37.53, "lng": 127.02, "level": "elevated", "note": "North Korean provocations, missile tests"},
    {"name": "South China Sea", "lat": 11.00, "lng": 114.00, "level": "elevated", "note": "Chinese territorial claims, Philippine confrontations"},
    {"name": "Iran — Israel", "lat": 32.43, "lng": 53.69, "level": "high", "note": "Shadow war, proxy conflicts, nuclear program"},
    {"name": "India — Pakistan", "lat": 34.08, "lng": 74.80, "level": "elevated", "note": "Kashmir tensions, periodic escalation"},
    {"name": "Nagorno-Karabakh / Armenia", "lat": 39.82, "lng": 46.76, "level": "watch", "note": "Post-2023 ethnic cleansing, border tensions"},
    {"name": "Venezuela — Guyana", "lat": 6.80, "lng": -58.16, "level": "watch", "note": "Essequibo territorial dispute"},
    {"name": "Arctic", "lat": 78.23, "lng": 15.65, "level": "watch", "note": "Russian militarization, NATO expansion"},
]


# ── Unconventional Indicators ────────────────────────────────────────────────
INDICATORS = [
    {
        "id": "pentagon-pizza",
        "name": "Pentagon Pizza Index",
        "value": 72,
        "max": 100,
        "unit": "",
        "status": "elevated",
        "description": "Late-night delivery activity near DoD/IC facilities. Spikes before major operations.",
        "source": "Proxy / anecdotal",
        "history": [30, 35, 28, 42, 55, 68, 72],
    },
    {
        "id": "doomsday-clock",
        "name": "Doomsday Clock",
        "value": 89,
        "max": 100,
        "unit": "sec to midnight",
        "display_value": "89 sec",
        "status": "critical",
        "description": "Bulletin of the Atomic Scientists\u2019 assessment of existential risk to humanity.",
        "source": "Bulletin of the Atomic Scientists",
        "history": [100, 120, 100, 90, 90, 90, 89],
    },
    {
        "id": "vix",
        "name": "VIX (Fear Index)",
        "value": 28.5,
        "max": 80,
        "unit": "",
        "status": "elevated",
        "description": "CBOE Volatility Index. Measures expected 30-day S&P 500 volatility. >20 = fear, >30 = panic.",
        "source": "CBOE",
        "history": [14, 16, 19, 22, 18, 24, 28.5],
    },
    {
        "id": "baltic-dry",
        "name": "Baltic Dry Index",
        "value": 1420,
        "max": 5000,
        "unit": "",
        "status": "normal",
        "description": "Global shipping cost index. Tracks demand for raw materials. Low = trade slowdown.",
        "source": "Baltic Exchange",
        "history": [1800, 1650, 1500, 1380, 1350, 1400, 1420],
    },
    {
        "id": "big-mac",
        "name": "Big Mac Index",
        "value": 5.69,
        "max": 10,
        "unit": "USD",
        "status": "normal",
        "description": "The Economist\u2019s PPP measure. Compares burger prices worldwide to gauge currency valuation.",
        "source": "The Economist",
        "history": [4.80, 5.15, 5.35, 5.58, 5.65, 5.69, 5.69],
    },
    {
        "id": "misery-index",
        "name": "Misery Index",
        "value": 7.2,
        "max": 25,
        "unit": "%",
        "status": "normal",
        "description": "Inflation rate + unemployment rate. Higher = more economic pain.",
        "source": "BLS / calculated",
        "history": [5.5, 6.8, 8.5, 10.2, 9.1, 7.8, 7.2],
    },
    {
        "id": "waffle-house",
        "name": "Waffle House Index",
        "value": 95,
        "max": 100,
        "unit": "% open",
        "status": "normal",
        "description": "FEMA\u2019s informal disaster metric. If Waffle House closes, it\u2019s serious.",
        "source": "FEMA concept / simulated",
        "history": [98, 99, 97, 93, 88, 94, 95],
    },
    {
        "id": "lipstick-index",
        "name": "Lipstick Index",
        "value": 62,
        "max": 100,
        "unit": "",
        "status": "elevated",
        "description": "Cosmetics sales as recession indicator. Rising sales = consumers trading down from big luxuries.",
        "source": "Est\u00e9e Lauder concept",
        "history": [45, 48, 52, 55, 58, 60, 62],
    },
    {
        "id": "cardboard-box",
        "name": "Cardboard Box Index",
        "value": 58,
        "max": 100,
        "unit": "",
        "status": "low",
        "description": "Corrugated box shipments track manufacturing and e-commerce. Falling = economic slowdown.",
        "source": "Fibre Box Association concept",
        "history": [72, 68, 65, 60, 56, 55, 58],
    },
    {
        "id": "underwear-index",
        "name": "Men\u2019s Underwear Index",
        "value": 44,
        "max": 100,
        "unit": "",
        "status": "low",
        "description": "Greenspan\u2019s indicator: men delay replacing underwear during downturns.",
        "source": "Alan Greenspan / retail data",
        "history": [65, 60, 55, 50, 48, 45, 44],
    },
    {
        "id": "copper-gold",
        "name": "Copper / Gold Ratio",
        "value": 0.18,
        "max": 0.5,
        "unit": "",
        "status": "low",
        "description": "Low ratio = fear (gold up, copper down). High = optimism (industrial demand strong).",
        "source": "Commodity markets",
        "history": [0.24, 0.22, 0.21, 0.20, 0.19, 0.18, 0.18],
    },
    {
        "id": "skyscraper-index",
        "name": "Skyscraper Index",
        "value": 68,
        "max": 100,
        "unit": "",
        "status": "elevated",
        "description": "Correlation between record-breaking skyscrapers and impending economic crashes.",
        "source": "Barclays concept",
        "history": [55, 58, 60, 63, 65, 67, 68],
    },
]


# ── Geopolitical Overview ─────────────────────────────────────────────────────
SANCTIONS = [
    {"from": "US / EU / UK", "target": "Russia", "since": "2022", "type": "Comprehensive", "note": "Energy, finance, tech, oligarchs"},
    {"from": "US / EU", "target": "Iran", "since": "2018 (reimposed)", "type": "Comprehensive", "note": "Oil, banking, nuclear-related"},
    {"from": "US", "target": "China (select)", "since": "2020+", "type": "Targeted", "note": "Chips, AI, defense entities"},
    {"from": "US / EU", "target": "North Korea", "since": "2006+", "type": "Comprehensive", "note": "Full economic isolation"},
    {"from": "US / EU", "target": "Myanmar", "since": "2021", "type": "Targeted", "note": "Military junta, arms embargo"},
    {"from": "US", "target": "Venezuela", "since": "2017+", "type": "Targeted", "note": "Oil sector, officials"},
    {"from": "US / EU", "target": "Syria", "since": "2011+", "type": "Comprehensive", "note": "Under review post-Assad"},
]

# ── Militias / Non-State Armed Groups ────────────────────────────────────────
MILITIAS = [
    {"name": "Hezbollah", "country": "Lebanon", "lat": 33.87, "lng": 35.51, "strength": "100 000+", "type": "Political militia", "backers": "Iran", "note": "Iranian proxy, dominant force in Lebanon"},
    {"name": "Wagner Group / Africa Corps", "country": "Russia / Africa", "lat": 13.52, "lng": 2.11, "strength": "20 000+", "type": "PMC", "backers": "Russia", "note": "Deployed in Mali, Niger, Burkina Faso, Libya, CAR, Sudan"},
    {"name": "Rapid Support Forces (RSF)", "country": "Sudan", "lat": 13.18, "lng": 30.22, "strength": "100 000+", "type": "Paramilitary", "backers": "UAE (alleged)", "note": "Former Janjaweed, now warring with Sudanese army"},
    {"name": "M23", "country": "DRC", "lat": -1.68, "lng": 29.22, "strength": "6 000+", "type": "Rebel group", "backers": "Rwanda", "note": "Controlling parts of eastern DRC, Goma captured"},
    {"name": "Houthis (Ansar Allah)", "country": "Yemen", "lat": 15.37, "lng": 44.19, "strength": "150 000+", "type": "Armed movement", "backers": "Iran", "note": "Controls northern Yemen, Red Sea shipping attacks"},
    {"name": "Hamas", "country": "Palestine", "lat": 31.42, "lng": 34.35, "strength": "30 000+ (pre-Oct 7)", "type": "Armed movement", "backers": "Iran, Qatar", "note": "Governing authority in Gaza, severely degraded since 2023"},
    {"name": "Taliban", "country": "Afghanistan", "lat": 34.53, "lng": 69.17, "strength": "80 000+", "type": "De facto government", "backers": "Pakistan (historical)", "note": "Controls Afghanistan since Aug 2021"},
    {"name": "Iraqi PMF (Al-Hashd)", "country": "Iraq", "lat": 33.31, "lng": 44.37, "strength": "100 000+", "type": "State militia", "backers": "Iran", "note": "Iranian-backed umbrella of Shia militias, nominally under Iraqi state"},
    {"name": "SDF / YPG", "country": "Syria (NE)", "lat": 36.51, "lng": 41.22, "strength": "60 000+", "type": "Ethnic militia", "backers": "US", "note": "Kurdish-led forces controlling NE Syria, anti-ISIS partner"},
    {"name": "JNIM (al-Qaeda Sahel)", "country": "Mali / Burkina Faso", "lat": 14.60, "lng": -1.50, "strength": "5 000+", "type": "Jihadist", "backers": "al-Qaeda", "note": "Al-Qaeda affiliate dominating Sahel insurgency"},
    {"name": "ISIS-Sahel (ISGS)", "country": "Niger / Mali / Burkina Faso", "lat": 13.50, "lng": 2.10, "strength": "3 000+", "type": "Jihadist", "backers": "ISIS", "note": "ISIS affiliate, competes with JNIM in Sahel"},
    {"name": "Al-Shabaab", "country": "Somalia", "lat": 2.05, "lng": 45.34, "strength": "10 000+", "type": "Jihadist", "backers": "al-Qaeda", "note": "Controls large rural areas of southern Somalia"},
    {"name": "Boko Haram / ISWAP", "country": "Nigeria", "lat": 11.85, "lng": 13.16, "strength": "5 000+", "type": "Jihadist", "backers": "ISIS (ISWAP)", "note": "NE Nigeria insurgency, split into factions"},
    {"name": "PDFs (People's Defence Forces)", "country": "Myanmar", "lat": 19.76, "lng": 96.07, "strength": "65 000+", "type": "Resistance militia", "backers": "NUG", "note": "Anti-junta resistance, allied with ethnic armed orgs"},
    {"name": "Amhara Fano", "country": "Ethiopia", "lat": 11.59, "lng": 37.39, "strength": "Unknown", "type": "Ethnic militia", "backers": "Self-organized", "note": "Amhara nationalist militia fighting Ethiopian federal govt"},
    {"name": "Sinaloa Cartel", "country": "Mexico", "lat": 24.81, "lng": -107.39, "strength": "20 000+", "type": "Criminal armed group", "backers": "Self-funded", "note": "One of two dominant Mexican drug cartels, controls vast territory"},
    {"name": "CJNG (Jalisco New Generation)", "country": "Mexico", "lat": 20.67, "lng": -103.35, "strength": "15 000+", "type": "Criminal armed group", "backers": "Self-funded", "note": "Most aggressive Mexican cartel, expanding operations"},
    {"name": "ELN", "country": "Colombia", "lat": 7.12, "lng": -73.12, "strength": "5 000+", "type": "Guerrilla", "backers": "Self-funded", "note": "Last major guerrilla group in Colombia, peace talks ongoing"},
]

# ── Country Relations ────────────────────────────────────────────────────────
COUNTRY_RELATIONS = {
    "US": {
        "name": "United States", "lat": 38.90, "lng": -77.04, "flag": "US",
        "relations": [
            {"target": "GB", "name": "United Kingdom", "lat": 51.51, "lng": -0.13, "type": "ally", "detail": "NATO, Five Eyes, 'Special Relationship'"},
            {"target": "CA", "name": "Canada", "lat": 45.42, "lng": -75.70, "type": "ally", "detail": "NATO, Five Eyes, USMCA trade"},
            {"target": "AU", "name": "Australia", "lat": -35.28, "lng": 149.13, "type": "ally", "detail": "AUKUS, Five Eyes, ANZUS"},
            {"target": "JP", "name": "Japan", "lat": 35.68, "lng": 139.69, "type": "ally", "detail": "Mutual defense treaty, Indo-Pacific strategy"},
            {"target": "KR", "name": "South Korea", "lat": 37.57, "lng": 126.98, "type": "ally", "detail": "Mutual defense treaty, 28k US troops stationed"},
            {"target": "DE", "name": "Germany", "lat": 52.52, "lng": 13.41, "type": "ally", "detail": "NATO, major trade partner"},
            {"target": "FR", "name": "France", "lat": 48.86, "lng": 2.35, "type": "ally", "detail": "NATO, oldest alliance"},
            {"target": "IL", "name": "Israel", "lat": 31.77, "lng": 35.21, "type": "ally", "detail": "$3.8B annual military aid, strategic partner"},
            {"target": "TW", "name": "Taiwan", "lat": 25.03, "lng": 121.57, "type": "partner", "detail": "Strategic ambiguity, major arms sales"},
            {"target": "UA", "name": "Ukraine", "lat": 50.45, "lng": 30.52, "type": "partner", "detail": "$175B+ aid since 2022, military support"},
            {"target": "IN", "name": "India", "lat": 28.61, "lng": 77.21, "type": "partner", "detail": "Quad member, growing defense ties"},
            {"target": "SA", "name": "Saudi Arabia", "lat": 24.71, "lng": 46.68, "type": "partner", "detail": "Oil, arms sales, complex partnership"},
            {"target": "CN", "name": "China", "lat": 39.90, "lng": 116.40, "type": "rival", "detail": "Strategic competition, trade war, tech restrictions"},
            {"target": "RU", "name": "Russia", "lat": 55.75, "lng": 37.62, "type": "adversary", "detail": "Sanctions, proxy conflict in Ukraine, nuclear tension"},
            {"target": "IR", "name": "Iran", "lat": 35.69, "lng": 51.39, "type": "adversary", "detail": "Sanctions, nuclear standoff, proxy conflicts"},
            {"target": "KP", "name": "North Korea", "lat": 39.02, "lng": 125.75, "type": "adversary", "detail": "Nuclear threat, full sanctions regime"},
        ]
    },
    "CN": {
        "name": "China", "lat": 39.90, "lng": 116.40, "flag": "CN",
        "relations": [
            {"target": "RU", "name": "Russia", "lat": 55.75, "lng": 37.62, "type": "ally", "detail": "'No limits' partnership, energy imports, UN alignment"},
            {"target": "PK", "name": "Pakistan", "lat": 33.69, "lng": 73.04, "type": "ally", "detail": "'Iron brothers', CPEC, military cooperation"},
            {"target": "KP", "name": "North Korea", "lat": 39.02, "lng": 125.75, "type": "ally", "detail": "Treaty ally, economic lifeline, buffer state"},
            {"target": "KH", "name": "Cambodia", "lat": 11.55, "lng": 104.92, "type": "partner", "detail": "BRI, military base (Ream), economic influence"},
            {"target": "SA", "name": "Saudi Arabia", "lat": 24.71, "lng": 46.68, "type": "partner", "detail": "Major oil supplier, growing ties, brokered Iran deal"},
            {"target": "IR", "name": "Iran", "lat": 35.69, "lng": 51.39, "type": "partner", "detail": "25-year cooperation deal, oil imports, strategic alignment"},
            {"target": "BR", "name": "Brazil", "lat": -15.79, "lng": -47.88, "type": "partner", "detail": "BRICS, major trade partner, commodities"},
            {"target": "US", "name": "United States", "lat": 38.90, "lng": -77.04, "type": "rival", "detail": "Trade war, tech competition, Taiwan strait tensions"},
            {"target": "JP", "name": "Japan", "lat": 35.68, "lng": 139.69, "type": "rival", "detail": "Historical tensions, Senkaku/Diaoyu islands dispute"},
            {"target": "IN", "name": "India", "lat": 28.61, "lng": 77.21, "type": "rival", "detail": "Border clashes (LAC), strategic competition"},
            {"target": "TW", "name": "Taiwan", "lat": 25.03, "lng": 121.57, "type": "adversary", "detail": "Claims sovereignty, military pressure, 'reunification'"},
            {"target": "PH", "name": "Philippines", "lat": 14.60, "lng": 120.98, "type": "rival", "detail": "South China Sea confrontations, reef disputes"},
            {"target": "AU", "name": "Australia", "lat": -35.28, "lng": 149.13, "type": "rival", "detail": "Trade sanctions, AUKUS opposition, influence competition"},
        ]
    },
    "RU": {
        "name": "Russia", "lat": 55.75, "lng": 37.62, "flag": "RU",
        "relations": [
            {"target": "CN", "name": "China", "lat": 39.90, "lng": 116.40, "type": "ally", "detail": "'No limits' partnership, energy exports, military drills"},
            {"target": "KP", "name": "North Korea", "lat": 39.02, "lng": 125.75, "type": "ally", "detail": "Weapons-for-tech deal, troops in Ukraine (alleged)"},
            {"target": "IR", "name": "Iran", "lat": 35.69, "lng": 51.39, "type": "ally", "detail": "Drone supplier, military cooperation, Syria alliance"},
            {"target": "BY", "name": "Belarus", "lat": 53.90, "lng": 27.57, "type": "ally", "detail": "Union State, military staging area, Lukashenko dependence"},
            {"target": "SY", "name": "Syria", "lat": 33.51, "lng": 36.29, "type": "ally", "detail": "Military bases, backed Assad regime"},
            {"target": "IN", "name": "India", "lat": 28.61, "lng": 77.21, "type": "partner", "detail": "Major arms buyer, discounted oil, historic ties"},
            {"target": "TR", "name": "Turkey", "lat": 39.93, "lng": 32.86, "type": "partner", "detail": "S-400 deal, energy hub, complex NATO-Russia bridge"},
            {"target": "US", "name": "United States", "lat": 38.90, "lng": -77.04, "type": "adversary", "detail": "Sanctions, nuclear tensions, proxy war in Ukraine"},
            {"target": "UA", "name": "Ukraine", "lat": 50.45, "lng": 30.52, "type": "adversary", "detail": "Active war, full-scale invasion since Feb 2022"},
            {"target": "GB", "name": "United Kingdom", "lat": 51.51, "lng": -0.13, "type": "adversary", "detail": "Sanctions, Litvinenko/Skripal poisonings, Ukraine support"},
            {"target": "PL", "name": "Poland", "lat": 52.23, "lng": 21.01, "type": "adversary", "detail": "NATO frontline, major Ukraine arms conduit"},
        ]
    },
    "IR": {
        "name": "Iran", "lat": 35.69, "lng": 51.39, "flag": "IR",
        "relations": [
            {"target": "RU", "name": "Russia", "lat": 55.75, "lng": 37.62, "type": "ally", "detail": "Drone/missile supply, military cooperation"},
            {"target": "CN", "name": "China", "lat": 39.90, "lng": 116.40, "type": "partner", "detail": "25-year pact, oil exports despite sanctions"},
            {"target": "SY", "name": "Syria", "lat": 33.51, "lng": 36.29, "type": "ally", "detail": "Axis of Resistance, Hezbollah land bridge (disrupted)"},
            {"target": "IQ", "name": "Iraq", "lat": 33.31, "lng": 44.37, "type": "partner", "detail": "Shia militia influence, PMF backing"},
            {"target": "IL", "name": "Israel", "lat": 31.77, "lng": 35.21, "type": "adversary", "detail": "Existential enmity, proxy wars, direct strikes in 2024"},
            {"target": "US", "name": "United States", "lat": 38.90, "lng": -77.04, "type": "adversary", "detail": "Comprehensive sanctions, nuclear standoff"},
            {"target": "SA", "name": "Saudi Arabia", "lat": 24.71, "lng": 46.68, "type": "rival", "detail": "Sectarian rivalry, proxy wars (Yemen), China-brokered detente"},
        ]
    },
    "IL": {
        "name": "Israel", "lat": 31.77, "lng": 35.21, "flag": "IL",
        "relations": [
            {"target": "US", "name": "United States", "lat": 38.90, "lng": -77.04, "type": "ally", "detail": "$3.8B/yr military aid, diplomatic shield"},
            {"target": "AE", "name": "UAE", "lat": 24.45, "lng": 54.65, "type": "partner", "detail": "Abraham Accords, trade, intelligence sharing"},
            {"target": "BH", "name": "Bahrain", "lat": 26.07, "lng": 50.56, "type": "partner", "detail": "Abraham Accords normalization"},
            {"target": "EG", "name": "Egypt", "lat": 30.04, "lng": 31.24, "type": "partner", "detail": "Camp David peace, Gaza border coordination"},
            {"target": "IR", "name": "Iran", "lat": 35.69, "lng": 51.39, "type": "adversary", "detail": "Shadow war, proxy conflicts, nuclear threat"},
            {"target": "LB", "name": "Lebanon (Hezbollah)", "lat": 33.87, "lng": 35.51, "type": "adversary", "detail": "2024 war, border conflict, Iranian proxy"},
            {"target": "PS", "name": "Palestine (Hamas)", "lat": 31.42, "lng": 34.35, "type": "adversary", "detail": "Gaza war since Oct 2023, occupation"},
        ]
    },
    "IN": {
        "name": "India", "lat": 28.61, "lng": 77.21, "flag": "IN",
        "relations": [
            {"target": "US", "name": "United States", "lat": 38.90, "lng": -77.04, "type": "partner", "detail": "Quad, defense deals, tech cooperation"},
            {"target": "JP", "name": "Japan", "lat": 35.68, "lng": 139.69, "type": "ally", "detail": "Quad, infrastructure investment, defense ties"},
            {"target": "FR", "name": "France", "lat": 48.86, "lng": 2.35, "type": "partner", "detail": "Rafale jets, submarine deal, strategic partnership"},
            {"target": "RU", "name": "Russia", "lat": 55.75, "lng": 37.62, "type": "partner", "detail": "Historic arms supplier, discounted oil imports"},
            {"target": "CN", "name": "China", "lat": 39.90, "lng": 116.40, "type": "rival", "detail": "LAC border clashes, strategic competition"},
            {"target": "PK", "name": "Pakistan", "lat": 33.69, "lng": 73.04, "type": "adversary", "detail": "Kashmir dispute, terror attacks, nuclear standoff"},
        ]
    },
    "UA": {
        "name": "Ukraine", "lat": 50.45, "lng": 30.52, "flag": "UA",
        "relations": [
            {"target": "US", "name": "United States", "lat": 38.90, "lng": -77.04, "type": "ally", "detail": "$175B+ total aid, ATACMS, F-16 support"},
            {"target": "GB", "name": "United Kingdom", "lat": 51.51, "lng": -0.13, "type": "ally", "detail": "Storm Shadow missiles, training, early supporter"},
            {"target": "PL", "name": "Poland", "lat": 52.23, "lng": 21.01, "type": "ally", "detail": "Logistics hub, refugee host, military aid"},
            {"target": "DE", "name": "Germany", "lat": 52.52, "lng": 13.41, "type": "ally", "detail": "Leopard tanks, major financial support"},
            {"target": "RU", "name": "Russia", "lat": 55.75, "lng": 37.62, "type": "adversary", "detail": "Active war, full-scale invasion since Feb 2022"},
            {"target": "BY", "name": "Belarus", "lat": 53.90, "lng": 27.57, "type": "adversary", "detail": "Staging ground for Russian invasion, hostile border"},
        ]
    },
}

# ── Country Profiles ─────────────────────────────────────────────────────────
# Comprehensive country data: leaders, policies, memberships, relation scores.
# Score scale 0-100: 0=hostile (red), 50=neutral (yellow), 100=close ally (green).
COUNTRY_PROFILES = {
    "US": {
        "name": "United States", "code": "US", "flag": "US",
        "lat": 38.90, "lng": -77.04, "capital": "Washington, D.C.",
        "population": "335M", "gdp": "$27.4T", "military_rank": 1,
        "govt": "Federal presidential republic",
        "leaders": [
            {"role": "President", "name": "Donald Trump (R)"},
            {"role": "Vice President", "name": "JD Vance"},
            {"role": "Sec. of State", "name": "Marco Rubio"},
            {"role": "Sec. of Defense", "name": "Pete Hegseth"},
        ],
        "memberships": ["UN (P5)", "NATO", "G7", "G20", "OECD", "WTO", "IMF", "World Bank", "Quad", "AUKUS", "Five Eyes", "USMCA"],
        "policies": {
            "abortion": {"status": "restricted", "detail": "Roe v. Wade overturned 2022; varies by state — banned/severely restricted in 14+ states"},
            "lgbtq_rights": {"status": "legal", "detail": "Same-sex marriage legal nationwide (Obergefell 2015); federal trans protections rolled back 2025"},
            "death_penalty": {"status": "active", "detail": "27 states retain; federal executions resumed under Trump admin"},
            "gun_control": {"status": "minimal", "detail": "2nd Amendment protection; state regs vary widely; 120 guns per 100 people"},
            "healthcare": {"status": "private-mixed", "detail": "Mixed private/Medicare/Medicaid; no universal coverage; ~8% uninsured"},
            "climate": {"status": "reduced", "detail": "Withdrew from Paris Agreement again (2025); fossil fuel expansion"},
            "immigration": {"status": "restrictive", "detail": "Mass deportation operations; border wall expansion; asylum restrictions"},
            "nuclear_stance": {"status": "armed", "detail": "5,044 warheads; modernizing triad; signatory to NPT"},
        },
        "relation_scores": {
            "GB": 96, "CA": 92, "AU": 94, "JP": 92, "KR": 88, "DE": 82, "FR": 80, "IL": 95,
            "IT": 80, "ES": 78, "NL": 85, "PL": 88, "NO": 85, "SE": 80, "FI": 82,
            "IN": 78, "TW": 85, "UA": 68, "SA": 70, "AE": 72, "EG": 68, "PH": 80, "SG": 82,
            "TR": 55, "BR": 58, "MX": 52, "AR": 62, "VN": 65, "ID": 60, "TH": 65, "ZA": 55,
            "CN": 22, "RU": 10, "IR": 8, "KP": 5, "SY": 15, "CU": 28, "VE": 22, "NI": 30, "BY": 15,
        }
    },
    "CN": {
        "name": "China", "code": "CN", "flag": "CN",
        "lat": 39.90, "lng": 116.40, "capital": "Beijing",
        "population": "1.41B", "gdp": "$17.8T", "military_rank": 3,
        "govt": "Marxist-Leninist one-party state",
        "leaders": [
            {"role": "President / CCP Gen. Sec.", "name": "Xi Jinping"},
            {"role": "Premier", "name": "Li Qiang"},
            {"role": "Foreign Minister", "name": "Wang Yi"},
        ],
        "memberships": ["UN (P5)", "WTO", "G20", "BRICS", "SCO", "APEC", "AIIB", "RCEP", "ASEAN+3"],
        "policies": {
            "abortion": {"status": "legal", "detail": "Widely available; history of forced abortions under one-child policy (1979-2015)"},
            "lgbtq_rights": {"status": "restricted", "detail": "Decriminalized 1997; no same-sex marriage; growing media censorship"},
            "death_penalty": {"status": "active", "detail": "Most executions globally (exact count a state secret); est. thousands/year"},
            "gun_control": {"status": "strict", "detail": "Civilian firearm ownership effectively banned"},
            "healthcare": {"status": "universal", "detail": "Basic medical insurance covers 95%+ of population"},
            "climate": {"status": "mixed", "detail": "Largest emitter but leading solar/EV producer; 2060 net-zero goal"},
            "immigration": {"status": "restrictive", "detail": "Very few foreign residents; difficult permanent status"},
            "nuclear_stance": {"status": "expanding", "detail": "500+ warheads (rapidly growing); signatory to NPT"},
        },
        "relation_scores": {
            "RU": 92, "PK": 94, "KP": 82, "IR": 82, "KH": 85, "LA": 85, "MM": 78, "VE": 78,
            "SA": 72, "BR": 75, "ZA": 78, "ET": 80, "HU": 70, "RS": 78, "CU": 80, "BY": 78,
            "ID": 68, "TH": 72, "MY": 70, "SG": 68,
            "DE": 55, "FR": 58, "IT": 60, "ES": 60,
            "US": 22, "JP": 28, "IN": 25, "KR": 45, "AU": 32, "PH": 30, "VN": 40,
            "GB": 35, "CA": 35, "LT": 10, "CZ": 25, "TW": 2,
        }
    },
    "RU": {
        "name": "Russia", "code": "RU", "flag": "RU",
        "lat": 55.75, "lng": 37.62, "capital": "Moscow",
        "population": "144M", "gdp": "$2.0T", "military_rank": 2,
        "govt": "Federal semi-presidential (de facto authoritarian)",
        "leaders": [
            {"role": "President", "name": "Vladimir Putin"},
            {"role": "Prime Minister", "name": "Mikhail Mishustin"},
            {"role": "Foreign Minister", "name": "Sergey Lavrov"},
            {"role": "Defense Minister", "name": "Andrey Belousov"},
        ],
        "memberships": ["UN (P5)", "BRICS", "CSTO", "SCO", "EAEU", "G20", "suspended: G8, CoE"],
        "policies": {
            "abortion": {"status": "legal", "detail": "Legal on request up to 12 weeks; some regional restrictions"},
            "lgbtq_rights": {"status": "banned", "detail": "'LGBT movement' labeled extremist 2023; gay propaganda ban; no marriage"},
            "death_penalty": {"status": "moratorium", "detail": "Constitutional moratorium since 1996 (discussions to reinstate)"},
            "gun_control": {"status": "strict", "detail": "Restrictive licensing; limited civilian ownership"},
            "healthcare": {"status": "universal", "detail": "State-funded universal coverage (quality varies)"},
            "climate": {"status": "low", "detail": "4th largest emitter; 2060 net-zero (weak enforcement)"},
            "immigration": {"status": "restrictive", "detail": "Labor migration from Central Asia; tightened after 2024 Crocus attack"},
            "nuclear_stance": {"status": "armed", "detail": "5,580 warheads (largest); modernizing; suspended New START 2023"},
        },
        "relation_scores": {
            "BY": 98, "CN": 92, "KP": 90, "IR": 92, "SY": 85, "KZ": 72, "AM": 65,
            "IN": 80, "TR": 62, "HU": 72, "RS": 82, "CU": 88, "VE": 88, "NI": 82,
            "BR": 65, "ZA": 62, "ET": 60,
            "US": 8, "GB": 8, "PL": 5, "UA": 2, "DE": 22, "FR": 25, "JP": 20, "CA": 12,
            "EE": 5, "LV": 5, "LT": 5, "FI": 10, "SE": 15, "NO": 15, "CZ": 10, "SK": 25,
        }
    },
    "GB": {
        "name": "United Kingdom", "code": "GB", "flag": "GB",
        "lat": 51.51, "lng": -0.13, "capital": "London",
        "population": "67M", "gdp": "$3.3T", "military_rank": 6,
        "govt": "Parliamentary constitutional monarchy",
        "leaders": [
            {"role": "Monarch", "name": "King Charles III"},
            {"role": "Prime Minister", "name": "Keir Starmer (Labour)"},
            {"role": "Foreign Secretary", "name": "David Lammy"},
        ],
        "memberships": ["UN (P5)", "NATO", "G7", "G20", "OECD", "WTO", "Commonwealth", "Five Eyes", "AUKUS", "CPTPP"],
        "policies": {
            "abortion": {"status": "legal", "detail": "Legal up to 24 weeks; free via NHS"},
            "lgbtq_rights": {"status": "legal", "detail": "Same-sex marriage since 2014; strong legal protections"},
            "death_penalty": {"status": "abolished", "detail": "Abolished 1965 (1998 for all crimes)"},
            "gun_control": {"status": "strict", "detail": "Handguns banned 1997; very low ownership"},
            "healthcare": {"status": "universal", "detail": "NHS — free at point of use, tax-funded"},
            "climate": {"status": "active", "detail": "Net-zero 2050 legally binding; coal phase-out complete"},
            "immigration": {"status": "mixed", "detail": "Post-Brexit points-based system; Rwanda scheme scrapped"},
            "nuclear_stance": {"status": "armed", "detail": "225 warheads; Trident submarines; raising cap to 260"},
        },
        "relation_scores": {
            "US": 96, "CA": 92, "AU": 94, "NZ": 92, "FR": 85, "DE": 85, "NL": 88, "IE": 88,
            "JP": 82, "KR": 78, "IL": 78, "NO": 88, "SE": 85, "DK": 85, "FI": 82, "PL": 85,
            "IN": 72, "SG": 85, "UA": 90, "TW": 68,
            "CN": 32, "RU": 8, "IR": 12, "KP": 5, "SY": 20, "VE": 30,
        }
    },
    "FR": {
        "name": "France", "code": "FR", "flag": "FR",
        "lat": 48.86, "lng": 2.35, "capital": "Paris",
        "population": "68M", "gdp": "$3.0T", "military_rank": 9,
        "govt": "Semi-presidential republic",
        "leaders": [
            {"role": "President", "name": "Emmanuel Macron"},
            {"role": "Prime Minister", "name": "Sébastien Lecornu"},
            {"role": "Foreign Minister", "name": "Jean-Noël Barrot"},
        ],
        "memberships": ["UN (P5)", "NATO", "EU", "G7", "G20", "OECD", "WTO", "Eurozone", "Schengen", "Francophonie"],
        "policies": {
            "abortion": {"status": "protected", "detail": "Enshrined in constitution (2024) — first country to do so; up to 14 weeks"},
            "lgbtq_rights": {"status": "legal", "detail": "Same-sex marriage since 2013"},
            "death_penalty": {"status": "abolished", "detail": "Abolished 1981; constitutional ban 2007"},
            "gun_control": {"status": "strict", "detail": "Tight licensing; ~19 guns per 100 people"},
            "healthcare": {"status": "universal", "detail": "PUMa — universal coverage; top-ranked by WHO"},
            "climate": {"status": "active", "detail": "70% nuclear electricity; 2050 net-zero"},
            "immigration": {"status": "restrictive", "detail": "2024 tightening law; ongoing debate on citizenship"},
            "nuclear_stance": {"status": "armed", "detail": "290 warheads; only EU nuclear power; independent deterrent"},
        },
        "relation_scores": {
            "DE": 92, "IT": 85, "ES": 85, "BE": 90, "NL": 82, "PT": 85, "IE": 80,
            "US": 80, "GB": 85, "CA": 85, "JP": 78, "IN": 82, "BR": 68,
            "MA": 72, "SN": 82, "CI": 75, "CM": 68, "TN": 70,
            "UA": 82, "PL": 78, "FI": 80, "SE": 82,
            "CN": 55, "RU": 20, "IR": 18, "KP": 5, "SY": 22, "ML": 15, "NE": 18, "BF": 20,
        }
    },
    "DE": {
        "name": "Germany", "code": "DE", "flag": "DE",
        "lat": 52.52, "lng": 13.41, "capital": "Berlin",
        "population": "84M", "gdp": "$4.5T", "military_rank": 19,
        "govt": "Federal parliamentary republic",
        "leaders": [
            {"role": "Chancellor", "name": "Friedrich Merz (CDU)"},
            {"role": "President", "name": "Frank-Walter Steinmeier"},
            {"role": "Foreign Minister", "name": "Johann Wadephul"},
        ],
        "memberships": ["UN", "NATO", "EU", "G7", "G20", "OECD", "WTO", "Eurozone", "Schengen"],
        "policies": {
            "abortion": {"status": "legal", "detail": "Legal up to 12 weeks with counseling requirement"},
            "lgbtq_rights": {"status": "legal", "detail": "Same-sex marriage since 2017"},
            "death_penalty": {"status": "abolished", "detail": "Abolished 1949 (West) / 1987 (East)"},
            "gun_control": {"status": "strict", "detail": "Tight licensing; one of strictest in EU"},
            "healthcare": {"status": "universal", "detail": "Statutory multi-payer insurance; 90% coverage"},
            "climate": {"status": "active", "detail": "Energiewende; net-zero 2045; nuclear phased out 2023"},
            "immigration": {"status": "mixed", "detail": "Skilled immigration law 2023; asylum debate intensifies"},
            "nuclear_stance": {"status": "non-nuclear", "detail": "No warheads; hosts US B61s (NATO sharing)"},
        },
        "relation_scores": {
            "FR": 92, "NL": 92, "AT": 95, "CH": 90, "BE": 88, "PL": 82, "DK": 88, "CZ": 85,
            "US": 82, "GB": 85, "CA": 85, "JP": 82, "KR": 78, "AU": 80, "IL": 78,
            "UA": 85, "IN": 72, "BR": 72,
            "CN": 50, "RU": 22, "IR": 18, "KP": 8, "SY": 20, "TR": 55,
        }
    },
    "JP": {
        "name": "Japan", "code": "JP", "flag": "JP",
        "lat": 35.68, "lng": 139.69, "capital": "Tokyo",
        "population": "124M", "gdp": "$4.2T", "military_rank": 7,
        "govt": "Parliamentary constitutional monarchy",
        "leaders": [
            {"role": "Emperor", "name": "Naruhito"},
            {"role": "Prime Minister", "name": "Sanae Takaichi (LDP)"},
            {"role": "Foreign Minister", "name": "Toshimitsu Motegi"},
        ],
        "memberships": ["UN", "G7", "G20", "OECD", "WTO", "Quad", "CPTPP", "APEC"],
        "policies": {
            "abortion": {"status": "legal", "detail": "Legal up to 22 weeks; spousal consent historically required"},
            "lgbtq_rights": {"status": "limited", "detail": "No same-sex marriage nationally; local partnership certificates; growing legal challenges"},
            "death_penalty": {"status": "active", "detail": "Retained; hangings conducted; ~100 on death row"},
            "gun_control": {"status": "strict", "detail": "Among strictest globally; very low ownership"},
            "healthcare": {"status": "universal", "detail": "Statutory health insurance; universal coverage"},
            "climate": {"status": "active", "detail": "Net-zero 2050; nuclear restarts post-Fukushima"},
            "immigration": {"status": "restrictive", "detail": "Very low foreign-born %; slowly opening to skilled workers"},
            "nuclear_stance": {"status": "non-nuclear", "detail": "Constitutional pacifism (Art. 9); under US nuclear umbrella"},
        },
        "relation_scores": {
            "US": 94, "AU": 88, "TW": 78, "PH": 80, "SG": 82, "IN": 82, "GB": 82, "FR": 78, "DE": 80,
            "TH": 78, "VN": 75, "ID": 72, "MY": 70, "CA": 85, "IT": 78,
            "KR": 55, "UA": 72,
            "CN": 28, "RU": 20, "KP": 5, "IR": 30,
        }
    },
    "IN": {
        "name": "India", "code": "IN", "flag": "IN",
        "lat": 28.61, "lng": 77.21, "capital": "New Delhi",
        "population": "1.44B", "gdp": "$3.9T", "military_rank": 4,
        "govt": "Federal parliamentary republic",
        "leaders": [
            {"role": "Prime Minister", "name": "Narendra Modi (BJP)"},
            {"role": "President", "name": "Droupadi Murmu"},
            {"role": "External Affairs Minister", "name": "S. Jaishankar"},
        ],
        "memberships": ["UN", "G20", "BRICS", "SCO", "Quad", "Commonwealth", "WTO", "SAARC"],
        "policies": {
            "abortion": {"status": "legal", "detail": "Legal up to 24 weeks (MTP Act 2021)"},
            "lgbtq_rights": {"status": "mixed", "detail": "Decriminalized 2018; no same-sex marriage"},
            "death_penalty": {"status": "active", "detail": "Retained; used for rarest cases"},
            "gun_control": {"status": "strict", "detail": "Arms Act — licensing required; low ownership"},
            "healthcare": {"status": "mixed", "detail": "Ayushman Bharat covers 500M poor; mostly out-of-pocket"},
            "climate": {"status": "mixed", "detail": "3rd largest emitter; 2070 net-zero; large solar push"},
            "immigration": {"status": "restrictive", "detail": "CAA 2019 controversial; refugee status complex"},
            "nuclear_stance": {"status": "armed", "detail": "172 warheads; not NPT signatory; 'no first use' policy"},
        },
        "relation_scores": {
            "RU": 82, "IL": 78, "FR": 85, "JP": 82, "US": 78, "AU": 78, "GB": 72, "DE": 72,
            "BR": 75, "ZA": 72, "VN": 75, "SG": 80, "ID": 70, "UAE": 82, "SA": 72,
            "AF": 40, "BD": 50, "NP": 62, "LK": 58, "IR": 55, "MM": 55,
            "CN": 22, "PK": 5, "CA": 35,
        }
    },
    "BR": {
        "name": "Brazil", "code": "BR", "flag": "BR",
        "lat": -15.79, "lng": -47.88, "capital": "Brasília",
        "population": "216M", "gdp": "$2.1T", "military_rank": 11,
        "govt": "Federal presidential republic",
        "leaders": [
            {"role": "President", "name": "Luiz Inácio Lula da Silva (PT)"},
            {"role": "Vice President", "name": "Geraldo Alckmin"},
            {"role": "Foreign Minister", "name": "Mauro Vieira"},
        ],
        "memberships": ["UN", "G20", "BRICS", "Mercosur", "OAS", "WTO", "CELAC"],
        "policies": {
            "abortion": {"status": "restricted", "detail": "Only legal for rape, life risk, anencephaly"},
            "lgbtq_rights": {"status": "legal", "detail": "Same-sex marriage since 2013; high violence rates"},
            "death_penalty": {"status": "abolished", "detail": "Abolished except military wartime"},
            "gun_control": {"status": "mixed", "detail": "Tightened under Lula; loosened under Bolsonaro era"},
            "healthcare": {"status": "universal", "detail": "SUS — universal public healthcare"},
            "climate": {"status": "mixed", "detail": "Amazon deforestation down under Lula; COP30 host 2025"},
            "immigration": {"status": "open", "detail": "Welcomed Venezuelan refugees; liberal policies"},
            "nuclear_stance": {"status": "non-nuclear", "detail": "NPT signatory; civilian nuclear program"},
        },
        "relation_scores": {
            "AR": 82, "UY": 88, "PY": 85, "CL": 85, "PT": 90, "ES": 82, "FR": 80, "CN": 78,
            "IN": 78, "ZA": 78, "RU": 65, "DE": 75, "IT": 75, "JP": 72, "US": 58, "GB": 70,
            "IR": 55, "CU": 72, "VE": 68,
            "KP": 25,
        }
    },
    "IL": {
        "name": "Israel", "code": "IL", "flag": "IL",
        "lat": 31.77, "lng": 35.21, "capital": "Jerusalem (contested)",
        "population": "9.9M", "gdp": "$530B", "military_rank": 17,
        "govt": "Parliamentary republic",
        "leaders": [
            {"role": "Prime Minister", "name": "Benjamin Netanyahu (Likud)"},
            {"role": "President", "name": "Isaac Herzog"},
            {"role": "Foreign Minister", "name": "Gideon Sa'ar"},
            {"role": "Defense Minister", "name": "Israel Katz"},
        ],
        "memberships": ["UN", "OECD", "WTO"],
        "policies": {
            "abortion": {"status": "legal", "detail": "Legal with committee approval; broadly accessible"},
            "lgbtq_rights": {"status": "mixed", "detail": "Civil unions; same-sex marriage not performed but recognized if abroad"},
            "death_penalty": {"status": "limited", "detail": "Only for Nazi war crimes and treason; used once (Eichmann 1962)"},
            "gun_control": {"status": "moderate", "detail": "Licensing required; widespread due to security situation"},
            "healthcare": {"status": "universal", "detail": "National Health Insurance Law 1995"},
            "climate": {"status": "mixed", "detail": "Net-zero 2050 target; fossil fuel dependent"},
            "immigration": {"status": "open-jewish", "detail": "Law of Return for Jews worldwide; restrictive otherwise"},
            "nuclear_stance": {"status": "undeclared", "detail": "~90 warheads (estimated); policy of opacity; not NPT signatory"},
        },
        "relation_scores": {
            "US": 95, "DE": 80, "GB": 75, "CA": 80, "IN": 82, "AZ": 85, "GR": 82, "CY": 82,
            "AE": 78, "BH": 75, "MA": 70, "EG": 65, "JO": 60, "FR": 65, "IT": 72, "AU": 82,
            "BR": 50, "RU": 35, "TR": 20,
            "IR": 2, "SY": 5, "LB": 5, "PS": 3, "IQ": 15, "YE": 8, "PK": 12,
        }
    },
    "IR": {
        "name": "Iran", "code": "IR", "flag": "IR",
        "lat": 35.69, "lng": 51.39, "capital": "Tehran",
        "population": "89M", "gdp": "$400B", "military_rank": 14,
        "govt": "Islamic theocratic republic",
        "leaders": [
            {"role": "Supreme Leader", "name": "Ali Khamenei"},
            {"role": "President", "name": "Masoud Pezeshkian"},
            {"role": "Foreign Minister", "name": "Abbas Araghchi"},
        ],
        "memberships": ["UN", "OIC", "OPEC", "BRICS (2024)", "SCO (2023)", "ECO"],
        "policies": {
            "abortion": {"status": "restricted", "detail": "Only permitted for medical reasons; heavily restricted"},
            "lgbtq_rights": {"status": "banned", "detail": "Criminalized; capital offense in some cases"},
            "death_penalty": {"status": "active", "detail": "One of highest execution rates globally; public hangings"},
            "gun_control": {"status": "strict", "detail": "Civilian ownership tightly controlled"},
            "healthcare": {"status": "universal", "detail": "Public health coverage ~90%; sanctions impact"},
            "climate": {"status": "low", "detail": "Not Paris signatory until 2016; high emissions per GDP"},
            "immigration": {"status": "complex", "detail": "Hosts 3M+ Afghan refugees"},
            "nuclear_stance": {"status": "threshold", "detail": "No declared warheads; 60% enrichment; IAEA disputes"},
        },
        "relation_scores": {
            "RU": 90, "CN": 82, "SY": 88, "IQ": 75, "VE": 82, "BY": 72, "CU": 75, "NI": 70,
            "KP": 72, "QA": 60, "TR": 55, "OM": 65, "LB": 82,
            "IN": 55, "PK": 62, "ZA": 60, "BR": 55,
            "US": 5, "IL": 2, "SA": 30, "BH": 22, "UAE": 42, "GB": 15, "FR": 20, "DE": 25,
        }
    },
    "KP": {
        "name": "North Korea", "code": "KP", "flag": "KP",
        "lat": 39.02, "lng": 125.75, "capital": "Pyongyang",
        "population": "26M", "gdp": "$18B (est.)", "military_rank": 34,
        "govt": "Juche totalitarian dictatorship",
        "leaders": [
            {"role": "Supreme Leader", "name": "Kim Jong Un"},
            {"role": "Premier", "name": "Kim Tok-hun"},
            {"role": "Foreign Minister", "name": "Choe Son Hui"},
        ],
        "memberships": ["UN"],
        "policies": {
            "abortion": {"status": "unclear", "detail": "Officially legal; practice reportedly common"},
            "lgbtq_rights": {"status": "unknown", "detail": "No legal recognition; regime denies existence"},
            "death_penalty": {"status": "active", "detail": "Public executions; wide range of 'offenses'"},
            "gun_control": {"status": "banned", "detail": "Civilian ownership prohibited"},
            "healthcare": {"status": "nominal", "detail": "Officially universal; collapsed in practice"},
            "climate": {"status": "unknown", "detail": "Minimal data; vulnerable to climate impacts"},
            "immigration": {"status": "closed", "detail": "Among most closed countries globally"},
            "nuclear_stance": {"status": "armed", "detail": "~50 warheads; ICBM tests; withdrew from NPT 2003"},
        },
        "relation_scores": {
            "RU": 92, "CN": 85, "IR": 72, "SY": 68, "BY": 62, "CU": 75, "VE": 65,
            "US": 3, "KR": 5, "JP": 3, "GB": 8, "FR": 10, "DE": 12, "AU": 8, "CA": 10, "IL": 5,
        }
    },
    "UA": {
        "name": "Ukraine", "code": "UA", "flag": "UA",
        "lat": 50.45, "lng": 30.52, "capital": "Kyiv",
        "population": "36M (pre-war)", "gdp": "$180B", "military_rank": 18,
        "govt": "Semi-presidential republic (wartime)",
        "leaders": [
            {"role": "President", "name": "Volodymyr Zelensky"},
            {"role": "Prime Minister", "name": "Denys Shmyhal"},
            {"role": "Foreign Minister", "name": "Andrii Sybiha"},
            {"role": "Commander-in-Chief (AF)", "name": "Oleksandr Syrskyi"},
        ],
        "memberships": ["UN", "Council of Europe", "WTO", "EU candidate (2022)", "NATO aspirant"],
        "policies": {
            "abortion": {"status": "legal", "detail": "Legal on request up to 12 weeks"},
            "lgbtq_rights": {"status": "mixed", "detail": "Decriminalized; no same-sex marriage; civil union bill debated"},
            "death_penalty": {"status": "abolished", "detail": "Abolished 2000 (CoE requirement)"},
            "gun_control": {"status": "relaxed-wartime", "detail": "Civilian arms expanded post-2022 invasion"},
            "healthcare": {"status": "universal", "detail": "Public healthcare; severe wartime strain"},
            "climate": {"status": "active", "detail": "Paris Agreement; reconstruction includes green goals"},
            "immigration": {"status": "emergency", "detail": "Millions displaced; refugees abroad"},
            "nuclear_stance": {"status": "non-nuclear", "detail": "Gave up Soviet arsenal 1994 (Budapest Memorandum)"},
        },
        "relation_scores": {
            "PL": 95, "LT": 95, "LV": 95, "EE": 95, "GB": 92, "US": 72, "CA": 92, "DE": 85,
            "FR": 82, "IT": 78, "CZ": 92, "NL": 88, "DK": 90, "FI": 92, "SE": 92, "NO": 90, "RO": 85,
            "MD": 92, "GE": 88, "JP": 82, "AU": 82, "KR": 75, "TR": 70,
            "BY": 5, "RU": 1,
        }
    },
    "SA": {
        "name": "Saudi Arabia", "code": "SA", "flag": "SA",
        "lat": 24.71, "lng": 46.68, "capital": "Riyadh",
        "population": "36M", "gdp": "$1.1T", "military_rank": 23,
        "govt": "Absolute monarchy",
        "leaders": [
            {"role": "King", "name": "Salman bin Abdulaziz"},
            {"role": "Crown Prince / PM", "name": "Mohammed bin Salman (MBS)"},
            {"role": "Foreign Minister", "name": "Prince Faisal bin Farhan"},
        ],
        "memberships": ["UN", "G20", "OPEC", "GCC", "OIC", "Arab League", "WTO", "BRICS (2024)"],
        "policies": {
            "abortion": {"status": "restricted", "detail": "Only to save mother's life or for severe defect"},
            "lgbtq_rights": {"status": "banned", "detail": "Criminalized; potential death penalty"},
            "death_penalty": {"status": "active", "detail": "Among highest rates globally; public beheadings"},
            "gun_control": {"status": "strict", "detail": "Government-controlled licensing"},
            "healthcare": {"status": "universal-citizens", "detail": "Free for Saudi citizens"},
            "climate": {"status": "mixed", "detail": "Vision 2030 green targets; major oil exporter"},
            "immigration": {"status": "kafala", "detail": "13M foreign workers under kafala system"},
            "nuclear_stance": {"status": "non-nuclear", "detail": "NPT signatory; civilian program pursued"},
        },
        "relation_scores": {
            "US": 72, "AE": 90, "BH": 92, "KW": 88, "OM": 82, "QA": 68, "EG": 85, "JO": 82,
            "PK": 85, "CN": 75, "IN": 75, "GB": 72, "FR": 72, "TR": 62,
            "RU": 62, "BR": 65,
            "IR": 30, "IL": 45, "SY": 35, "YE": 15, "KP": 20, "VE": 40,
        }
    },
    "TR": {
        "name": "Turkey", "code": "TR", "flag": "TR",
        "lat": 39.93, "lng": 32.86, "capital": "Ankara",
        "population": "85M", "gdp": "$1.1T", "military_rank": 8,
        "govt": "Presidential republic",
        "leaders": [
            {"role": "President", "name": "Recep Tayyip Erdoğan (AKP)"},
            {"role": "Vice President", "name": "Cevdet Yılmaz"},
            {"role": "Foreign Minister", "name": "Hakan Fidan"},
        ],
        "memberships": ["UN", "NATO", "G20", "OECD", "OIC", "WTO", "Council of Europe", "EU candidate"],
        "policies": {
            "abortion": {"status": "legal-limited", "detail": "Legal up to 10 weeks; access restricted in practice"},
            "lgbtq_rights": {"status": "restricted", "detail": "Decriminalized; pride banned since 2015"},
            "death_penalty": {"status": "abolished", "detail": "Abolished 2004 (EU accession); reinstatement threatened"},
            "gun_control": {"status": "moderate", "detail": "Licensing required"},
            "healthcare": {"status": "universal", "detail": "SGK universal insurance"},
            "climate": {"status": "mixed", "detail": "Paris Agreement ratified 2021; 2053 net-zero"},
            "immigration": {"status": "complex", "detail": "Hosts 3.6M Syrian refugees; increasing deportations"},
            "nuclear_stance": {"status": "non-nuclear", "detail": "NPT signatory; hosts US B61s (NATO sharing)"},
        },
        "relation_scores": {
            "AZ": 98, "QA": 88, "PK": 82, "LY": 72, "SO": 78, "UZ": 80, "KZ": 75,
            "RU": 62, "CN": 55, "IR": 55, "UA": 72, "DE": 55, "GB": 62, "IT": 65, "JP": 68,
            "US": 55, "FR": 45, "IL": 20, "GR": 25, "CY": 15, "AM": 15, "SY": 25,
        }
    },
    "KR": {
        "name": "South Korea", "code": "KR", "flag": "KR",
        "lat": 37.57, "lng": 126.98, "capital": "Seoul",
        "population": "52M", "gdp": "$1.7T", "military_rank": 5,
        "govt": "Presidential republic",
        "leaders": [
            {"role": "President", "name": "Lee Jae-myung (DP)"},
            {"role": "Prime Minister", "name": "Kim Min-seok"},
            {"role": "Foreign Minister", "name": "Cho Hyun"},
        ],
        "memberships": ["UN", "G20", "OECD", "WTO", "APEC", "RCEP"],
        "policies": {
            "abortion": {"status": "legal", "detail": "Decriminalized 2021 (previously restricted)"},
            "lgbtq_rights": {"status": "limited", "detail": "Legal but no marriage or civil unions"},
            "death_penalty": {"status": "moratorium", "detail": "De facto moratorium since 1997"},
            "gun_control": {"status": "strict", "detail": "Very strict; low ownership"},
            "healthcare": {"status": "universal", "detail": "NHIS — single-payer universal coverage"},
            "climate": {"status": "active", "detail": "2050 net-zero; nuclear + renewable push"},
            "immigration": {"status": "restrictive", "detail": "Very low foreign-born %; aging crisis"},
            "nuclear_stance": {"status": "non-nuclear", "detail": "NPT signatory; under US nuclear umbrella; 'nuclear options' debate"},
        },
        "relation_scores": {
            "US": 92, "JP": 60, "AU": 80, "PH": 78, "VN": 78, "IN": 72, "SG": 82, "TW": 72,
            "DE": 72, "GB": 72, "FR": 70, "CA": 78, "UA": 65,
            "CN": 45, "RU": 28,
            "KP": 8, "IR": 25,
        }
    },
    "CA": {
        "name": "Canada", "code": "CA", "flag": "CA",
        "lat": 45.42, "lng": -75.70, "capital": "Ottawa",
        "population": "40M", "gdp": "$2.1T", "military_rank": 27,
        "govt": "Federal parliamentary constitutional monarchy",
        "leaders": [
            {"role": "Monarch", "name": "King Charles III"},
            {"role": "Prime Minister", "name": "Mark Carney (Liberal)"},
            {"role": "Gov. General", "name": "Mary Simon"},
            {"role": "Foreign Minister", "name": "Anita Anand"},
        ],
        "memberships": ["UN", "NATO", "G7", "G20", "OECD", "Commonwealth", "Five Eyes", "USMCA", "CPTPP", "Francophonie"],
        "policies": {
            "abortion": {"status": "legal", "detail": "No legal restrictions; covered by provincial health plans"},
            "lgbtq_rights": {"status": "legal", "detail": "Same-sex marriage since 2005; strong protections"},
            "death_penalty": {"status": "abolished", "detail": "Abolished 1976"},
            "gun_control": {"status": "moderate", "detail": "2022 handgun freeze; assault weapons banned"},
            "healthcare": {"status": "universal", "detail": "Single-payer provincial Medicare"},
            "climate": {"status": "active", "detail": "2050 net-zero; carbon tax; tensions with oil provinces"},
            "immigration": {"status": "open", "detail": "500k+ annual immigrants; points-based system"},
            "nuclear_stance": {"status": "non-nuclear", "detail": "Gave up weapons program; NPT signatory"},
        },
        "relation_scores": {
            "US": 82, "GB": 92, "FR": 88, "DE": 85, "AU": 92, "NZ": 90, "JP": 85, "KR": 78,
            "IT": 80, "NL": 85, "NO": 85, "MX": 70, "UA": 92, "PL": 85,
            "CN": 28, "RU": 10, "IR": 12, "KP": 8, "SA": 50, "IL": 68, "IN": 38,
        }
    },
    "AU": {
        "name": "Australia", "code": "AU", "flag": "AU",
        "lat": -35.28, "lng": 149.13, "capital": "Canberra",
        "population": "27M", "gdp": "$1.7T", "military_rank": 16,
        "govt": "Federal parliamentary constitutional monarchy",
        "leaders": [
            {"role": "Monarch", "name": "King Charles III"},
            {"role": "Prime Minister", "name": "Anthony Albanese (Labor)"},
            {"role": "Gov. General", "name": "Sam Mostyn"},
            {"role": "Foreign Minister", "name": "Penny Wong"},
        ],
        "memberships": ["UN", "G20", "OECD", "Commonwealth", "Five Eyes", "Quad", "AUKUS", "ANZUS", "CPTPP", "APEC"],
        "policies": {
            "abortion": {"status": "legal", "detail": "Legal in all states/territories (last: WA 2023)"},
            "lgbtq_rights": {"status": "legal", "detail": "Same-sex marriage since 2017"},
            "death_penalty": {"status": "abolished", "detail": "Abolished 1973 (fed) / 1984 (all)"},
            "gun_control": {"status": "strict", "detail": "Post-Port Arthur 1996 buyback; very strict"},
            "healthcare": {"status": "universal", "detail": "Medicare — universal public coverage"},
            "climate": {"status": "active", "detail": "43% emissions cut by 2030; 2050 net-zero"},
            "immigration": {"status": "points-based", "detail": "Skilled migration focus; offshore processing controversial"},
            "nuclear_stance": {"status": "non-nuclear", "detail": "NPT signatory; AUKUS nuclear-powered subs (not armed)"},
        },
        "relation_scores": {
            "US": 94, "GB": 94, "NZ": 98, "CA": 92, "JP": 88, "IN": 78, "KR": 80, "SG": 85,
            "FR": 68, "DE": 78, "PH": 82, "ID": 75, "VN": 72, "TW": 72, "MY": 72, "TH": 72,
            "PG": 92, "FJ": 85, "UA": 82, "IL": 75,
            "CN": 32, "RU": 15, "IR": 18, "KP": 8,
        }
    },
}


def _p(name, code, lat, lng, capital, pop, gdp, mil, govt, leaders, mems, policies, scores):
    """Compact constructor for country profiles."""
    return {
        "name": name, "code": code, "flag": code, "lat": lat, "lng": lng,
        "capital": capital, "population": pop, "gdp": gdp, "military_rank": mil,
        "govt": govt,
        "leaders": [{"role": r, "name": n} for r, n in leaders],
        "memberships": mems,
        "policies": {k: {"status": s, "detail": d} for k, (s, d) in policies.items()},
        "relation_scores": scores,
    }


# ── Rest of the world: comprehensive country profiles ───────────────────────
# Each country includes alliance/bloc memberships prominently.

COUNTRY_PROFILES.update({
    # ═══════════════ EUROPE (non-existing) ═══════════════
    "IT": _p("Italy", "IT", 41.90, 12.50, "Rome", "59M", "$2.3T", 10,
        "Parliamentary republic",
        [("President", "Sergio Mattarella"), ("Prime Minister", "Giorgia Meloni (FdI)"), ("Foreign Minister", "Antonio Tajani")],
        ["UN", "NATO", "EU", "G7", "G20", "OECD", "WTO", "Eurozone", "Schengen", "Council of Europe"],
        {"abortion": ("legal", "Legal up to 90 days; high conscientious objection"),
         "lgbtq_rights": ("mixed", "Civil unions 2016; no full marriage; Meloni rolling back"),
         "death_penalty": ("abolished", "Abolished 1948 (all crimes 1994)"),
         "gun_control": ("moderate", "Licensing required; moderate ownership"),
         "healthcare": ("universal", "SSN — tax-funded universal coverage"),
         "climate": ("active", "EU Green Deal; 2050 net-zero"),
         "immigration": ("restrictive", "Meloni crackdown; Albania deportation deal"),
         "nuclear_stance": ("non-nuclear", "NPT; hosts US B61s (NATO sharing)")},
        {"DE": 88, "FR": 85, "ES": 88, "GR": 78, "MT": 85, "US": 82, "GB": 80, "CA": 82, "JP": 78, "AU": 75,
         "UA": 80, "PL": 80, "AT": 85, "CH": 82, "SI": 82, "HR": 80, "AL": 72, "TN": 62, "LY": 52, "EG": 62,
         "IL": 72, "IN": 70, "BR": 72, "TR": 58, "CN": 55, "RU": 22, "IR": 25, "KP": 8, "SY": 22}),
    "ES": _p("Spain", "ES", 40.42, -3.70, "Madrid", "48M", "$1.6T", 18,
        "Parliamentary constitutional monarchy",
        [("Monarch", "King Felipe VI"), ("Prime Minister", "Pedro Sánchez (PSOE)"), ("Foreign Minister", "José Manuel Albares")],
        ["UN", "NATO", "EU", "G20", "OECD", "WTO", "Eurozone", "Schengen", "Ibero-American"],
        {"abortion": ("legal", "Legal up to 14 weeks; menstrual leave 2023"),
         "lgbtq_rights": ("legal", "Same-sex marriage since 2005; trans self-ID law"),
         "death_penalty": ("abolished", "Abolished 1978/1995"),
         "gun_control": ("strict", "Strict licensing"),
         "healthcare": ("universal", "SNS — universal coverage"),
         "climate": ("active", "Solar/wind leader; 2050 net-zero"),
         "immigration": ("mixed", "Regularization programs; Canary Islands crisis"),
         "nuclear_stance": ("non-nuclear", "NPT; no weapons program")},
        {"PT": 95, "FR": 85, "IT": 88, "DE": 82, "MX": 82, "AR": 88, "CL": 85, "CO": 82, "PE": 82, "BR": 82,
         "US": 78, "GB": 72, "MA": 70, "EU": 90, "UA": 80, "PL": 72, "NL": 82, "BE": 82, "IE": 78, "GR": 78,
         "CN": 52, "RU": 22, "IR": 25, "KP": 10, "VE": 45, "CU": 62, "IL": 58}),
    "PL": _p("Poland", "PL", 52.23, 21.01, "Warsaw", "38M", "$860B", 17,
        "Parliamentary republic",
        [("President", "Karol Nawrocki"), ("Prime Minister", "Donald Tusk (KO)"), ("Foreign Minister", "Radosław Sikorski")],
        ["UN", "NATO", "EU", "OECD", "WTO", "Visegrád 4", "Schengen", "Council of Europe"],
        {"abortion": ("restricted", "Near-total ban since 2021; limited exceptions"),
         "lgbtq_rights": ("restricted", "No civil unions; 'LGBT-free zones' disputed"),
         "death_penalty": ("abolished", "Abolished 1998 (EU accession)"),
         "gun_control": ("strict", "Tight licensing; civilian expansion planned"),
         "healthcare": ("universal", "NFZ — public coverage; supplementary private"),
         "climate": ("reluctant", "Still coal-dependent; EU ETS compliance"),
         "immigration": ("restrictive", "Belarus border wall; Ukraine refugee host"),
         "nuclear_stance": ("non-nuclear", "NPT; NATO nuclear sharing discussions")},
        {"US": 92, "GB": 92, "UA": 95, "LT": 95, "LV": 95, "EE": 95, "DE": 75, "FR": 75, "CZ": 90, "SK": 88,
         "HU": 72, "RO": 85, "FI": 88, "SE": 85, "NO": 82, "DK": 82, "CA": 85, "AU": 78, "JP": 75, "KR": 75,
         "TR": 68, "IL": 72, "IN": 62, "BR": 58, "CN": 32, "RU": 5, "BY": 8, "IR": 15, "KP": 8}),
    "NL": _p("Netherlands", "NL", 52.37, 4.89, "Amsterdam", "17.8M", "$1.1T", 37,
        "Parliamentary constitutional monarchy",
        [("Monarch", "King Willem-Alexander"), ("Prime Minister", "Dick Schoof"), ("Foreign Minister", "Caspar Veldkamp")],
        ["UN", "NATO", "EU", "OECD", "WTO", "Eurozone", "Schengen", "Benelux"],
        {"abortion": ("legal", "Legal up to 24 weeks; free via insurance"),
         "lgbtq_rights": ("legal", "First country to legalize same-sex marriage (2001)"),
         "death_penalty": ("abolished", "Abolished 1870/1982"),
         "gun_control": ("strict", "Tight licensing; very low ownership"),
         "healthcare": ("universal", "Mandatory private insurance; top-ranked"),
         "climate": ("active", "2050 net-zero; offshore wind leader"),
         "immigration": ("mixed", "Stricter under Schoof; labor migration"),
         "nuclear_stance": ("non-nuclear", "NPT; hosts US B61s (NATO sharing)")},
        {"DE": 92, "BE": 95, "LU": 95, "FR": 85, "GB": 88, "US": 85, "CA": 85, "AU": 82, "NO": 88, "DK": 88,
         "SE": 85, "FI": 82, "IT": 82, "ES": 82, "UA": 88, "PL": 78, "JP": 80, "KR": 78, "NZ": 82, "IE": 82,
         "IL": 72, "IN": 68, "CN": 50, "RU": 20, "IR": 18, "KP": 8, "TR": 55}),
    "BE": _p("Belgium", "BE", 50.85, 4.35, "Brussels", "11.7M", "$620B", 43,
        "Federal parliamentary constitutional monarchy",
        [("Monarch", "King Philippe"), ("Prime Minister", "Bart De Wever (N-VA)"), ("Foreign Minister", "Maxime Prévot")],
        ["UN", "NATO", "EU", "OECD", "WTO", "Eurozone", "Schengen", "Benelux", "Council of Europe"],
        {"abortion": ("legal", "Legal up to 12 weeks"),
         "lgbtq_rights": ("legal", "Same-sex marriage since 2003"),
         "death_penalty": ("abolished", "Abolished 1996"),
         "gun_control": ("strict", "Strict licensing"),
         "healthcare": ("universal", "Compulsory insurance"),
         "climate": ("active", "EU Green Deal compliance"),
         "immigration": ("mixed", "EU rules; asylum debate"),
         "nuclear_stance": ("non-nuclear", "NPT; hosts US B61s (NATO sharing)")},
        {"NL": 95, "LU": 95, "FR": 90, "DE": 88, "GB": 85, "US": 82, "IT": 82, "ES": 82, "CA": 82, "JP": 78,
         "UA": 82, "PL": 75, "AT": 82, "CH": 82, "IE": 80, "DK": 82, "SE": 82, "NO": 82,
         "CN": 52, "RU": 18, "IR": 20, "KP": 8, "IL": 58, "TR": 55, "IN": 65},
        ),
    "CH": _p("Switzerland", "CH", 46.95, 7.45, "Bern", "8.8M", "$900B", 31,
        "Federal semi-direct democracy",
        [("Federal President", "Karin Keller-Sutter"), ("Foreign Minister", "Ignazio Cassis")],
        ["UN", "OECD", "WTO", "EFTA", "Schengen", "Council of Europe"],
        {"abortion": ("legal", "Legal up to 12 weeks"),
         "lgbtq_rights": ("legal", "Same-sex marriage since 2022"),
         "death_penalty": ("abolished", "Abolished 1942/1992"),
         "gun_control": ("moderate", "Militia-based; high ownership but strict storage"),
         "healthcare": ("universal", "Mandatory private insurance"),
         "climate": ("active", "2050 net-zero; glacier retreat concern"),
         "immigration": ("restrictive", "Quota system; high foreign-born %"),
         "nuclear_stance": ("non-nuclear", "Neutrality; signed TPNW")},
        {"DE": 92, "AT": 95, "IT": 85, "FR": 85, "LI": 98, "US": 75, "GB": 78, "EU": 82, "NL": 82, "BE": 82,
         "JP": 75, "KR": 72, "CA": 78, "AU": 72, "UA": 72,
         "CN": 55, "RU": 30, "IR": 38, "KP": 15, "IL": 58, "TR": 52}),
    "AT": _p("Austria", "AT", 48.21, 16.37, "Vienna", "9M", "$530B", 36,
        "Federal parliamentary republic",
        [("President", "Alexander Van der Bellen"), ("Chancellor", "Christian Stocker (ÖVP)"), ("Foreign Minister", "Beate Meinl-Reisinger")],
        ["UN", "EU", "OECD", "OSCE", "Eurozone", "Schengen", "Council of Europe"],
        {"abortion": ("legal", "Legal up to 3 months"),
         "lgbtq_rights": ("legal", "Same-sex marriage since 2019"),
         "death_penalty": ("abolished", "Abolished 1950/1968"),
         "gun_control": ("moderate", "Licensing; moderate ownership"),
         "healthcare": ("universal", "Statutory insurance; high quality"),
         "climate": ("active", "2040 net-zero target"),
         "immigration": ("mixed", "Stricter asylum; skilled migration"),
         "nuclear_stance": ("non-nuclear", "Constitutional neutrality; TPNW signatory")},
        {"DE": 98, "CH": 95, "IT": 85, "CZ": 88, "SK": 85, "HU": 78, "SI": 88, "HR": 82, "FR": 78, "NL": 82,
         "BE": 80, "PL": 72, "US": 70, "GB": 72, "UA": 72, "JP": 72,
         "CN": 52, "RU": 28, "IR": 25, "KP": 10, "IL": 60, "TR": 45}),
    "SE": _p("Sweden", "SE", 59.33, 18.07, "Stockholm", "10.6M", "$620B", 25,
        "Parliamentary constitutional monarchy",
        [("Monarch", "King Carl XVI Gustaf"), ("Prime Minister", "Ulf Kristersson (Moderates)"), ("Foreign Minister", "Maria Malmer Stenergard")],
        ["UN", "NATO (2024)", "EU", "OECD", "WTO", "Schengen", "Nordic Council"],
        {"abortion": ("legal", "Legal up to 18 weeks; free"),
         "lgbtq_rights": ("legal", "Same-sex marriage since 2009"),
         "death_penalty": ("abolished", "Abolished 1921/1972"),
         "gun_control": ("moderate", "Licensing; hunting tradition"),
         "healthcare": ("universal", "Tax-funded universal coverage"),
         "climate": ("active", "2045 net-zero — most ambitious EU target"),
         "immigration": ("restrictive", "Recent tightening after mass inflows"),
         "nuclear_stance": ("non-nuclear", "NPT; joined NATO 2024")},
        {"NO": 95, "DK": 95, "FI": 98, "IS": 90, "DE": 82, "NL": 85, "GB": 85, "US": 82, "CA": 85, "EE": 90,
         "LV": 90, "LT": 90, "PL": 85, "UA": 92, "FR": 78, "IT": 75, "ES": 78, "JP": 80, "KR": 78, "AU": 78,
         "CN": 28, "RU": 8, "IR": 18, "KP": 8, "IL": 55, "TR": 45, "IN": 65}),
    "NO": _p("Norway", "NO", 59.91, 10.75, "Oslo", "5.5M", "$500B", 33,
        "Parliamentary constitutional monarchy",
        [("Monarch", "King Harald V"), ("Prime Minister", "Jonas Gahr Støre (Labour)"), ("Foreign Minister", "Espen Barth Eide")],
        ["UN", "NATO", "OECD", "WTO", "EFTA", "Schengen", "Arctic Council", "Nordic Council"],
        {"abortion": ("legal", "Legal up to 18 weeks (2024 expansion)"),
         "lgbtq_rights": ("legal", "Same-sex marriage since 2009"),
         "death_penalty": ("abolished", "Abolished 1905/1979"),
         "gun_control": ("strict", "Strict after 2011 Breivik attack"),
         "healthcare": ("universal", "Tax-funded universal; high quality"),
         "climate": ("active", "2030 -55%; but major oil/gas exporter"),
         "immigration": ("restrictive", "Stricter post-2015; Schengen member"),
         "nuclear_stance": ("non-nuclear", "NPT; TPNW observer")},
        {"SE": 95, "DK": 95, "FI": 92, "IS": 92, "GB": 92, "US": 88, "DE": 85, "NL": 85, "FR": 78, "CA": 88,
         "PL": 82, "UA": 92, "EE": 88, "LV": 88, "LT": 88, "AU": 80, "JP": 78, "KR": 75, "IT": 72,
         "CN": 30, "RU": 10, "IR": 20, "KP": 8, "IL": 52, "IN": 60}),
    "DK": _p("Denmark", "DK", 55.68, 12.57, "Copenhagen", "5.9M", "$400B", 47,
        "Parliamentary constitutional monarchy",
        [("Monarch", "King Frederik X"), ("Prime Minister", "Mette Frederiksen (Social Democrats)"), ("Foreign Minister", "Lars Løkke Rasmussen")],
        ["UN", "NATO", "EU", "OECD", "WTO", "Schengen", "Nordic Council", "Arctic Council"],
        {"abortion": ("legal", "Legal up to 18 weeks (2024 expansion)"),
         "lgbtq_rights": ("legal", "Same-sex marriage since 2012; pioneer"),
         "death_penalty": ("abolished", "Abolished 1930/1978"),
         "gun_control": ("strict", "Strict licensing"),
         "healthcare": ("universal", "Tax-funded; high quality"),
         "climate": ("active", "70% emissions cut by 2030"),
         "immigration": ("restrictive", "One of EU's strictest since 2015"),
         "nuclear_stance": ("non-nuclear", "NPT; no weapons on Danish soil policy")},
        {"SE": 95, "NO": 95, "FI": 90, "IS": 92, "DE": 85, "NL": 88, "GB": 88, "US": 85, "FR": 82, "CA": 85,
         "PL": 82, "UA": 92, "EE": 88, "LV": 88, "LT": 88, "JP": 78, "KR": 75, "AU": 80,
         "CN": 28, "RU": 10, "IR": 15, "KP": 8, "IL": 52, "GL": 70},
        ),
    "FI": _p("Finland", "FI", 60.17, 24.94, "Helsinki", "5.6M", "$310B", 51,
        "Parliamentary republic",
        [("President", "Alexander Stubb"), ("Prime Minister", "Petteri Orpo (NCP)"), ("Foreign Minister", "Elina Valtonen")],
        ["UN", "NATO (2023)", "EU", "OECD", "WTO", "Eurozone", "Schengen", "Nordic Council", "Arctic Council"],
        {"abortion": ("legal", "Legal on broad grounds; reform 2023"),
         "lgbtq_rights": ("legal", "Same-sex marriage since 2017"),
         "death_penalty": ("abolished", "Abolished 1826 (peace) / 1972"),
         "gun_control": ("moderate", "Hunting tradition; moderate ownership"),
         "healthcare": ("universal", "Universal public; top-ranked"),
         "climate": ("active", "2035 net-zero — most ambitious globally"),
         "immigration": ("restrictive", "Closed Russia border 2023"),
         "nuclear_stance": ("non-nuclear", "NPT; joined NATO 2023")},
        {"SE": 98, "NO": 92, "DK": 90, "IS": 85, "EE": 95, "LV": 88, "LT": 88, "DE": 82, "GB": 82, "US": 85,
         "NL": 82, "PL": 88, "UA": 92, "FR": 75, "CA": 82, "JP": 78, "KR": 72, "AU": 75,
         "CN": 25, "RU": 2, "IR": 15, "KP": 5, "IL": 55, "IN": 58}),
    "IE": _p("Ireland", "IE", 53.35, -6.26, "Dublin", "5.3M", "$570B", 99,
        "Parliamentary republic",
        [("President", "Michael D. Higgins"), ("Taoiseach", "Micheál Martin (FF)"), ("Tánaiste", "Simon Harris (FG)")],
        ["UN", "EU", "OECD", "WTO", "Eurozone", "Council of Europe"],
        {"abortion": ("legal", "Legalized 2018 referendum"),
         "lgbtq_rights": ("legal", "Same-sex marriage via referendum 2015"),
         "death_penalty": ("abolished", "Abolished 1990"),
         "gun_control": ("strict", "Very strict licensing"),
         "healthcare": ("mixed", "HSE public + private; reforms ongoing"),
         "climate": ("active", "2050 net-zero target"),
         "immigration": ("mixed", "Open EU; recent asylum debates"),
         "nuclear_stance": ("non-nuclear", "Constitutional neutrality; TPNW signatory")},
        {"GB": 85, "US": 92, "CA": 90, "AU": 88, "FR": 85, "DE": 85, "ES": 85, "IT": 82, "NL": 82,
         "PT": 80, "PL": 75, "UA": 82, "NZ": 85,
         "CN": 52, "RU": 20, "IR": 28, "KP": 10, "IL": 40},
        ),
    "PT": _p("Portugal", "PT", 38.72, -9.14, "Lisbon", "10.3M", "$290B", 57,
        "Parliamentary republic",
        [("President", "Marcelo Rebelo de Sousa"), ("Prime Minister", "Luís Montenegro (PSD)"), ("Foreign Minister", "Paulo Rangel")],
        ["UN", "NATO", "EU", "OECD", "WTO", "Eurozone", "Schengen", "CPLP (Lusophone)"],
        {"abortion": ("legal", "Legal up to 10 weeks since 2007"),
         "lgbtq_rights": ("legal", "Same-sex marriage since 2010"),
         "death_penalty": ("abolished", "Abolished 1867 (first in Europe)"),
         "gun_control": ("strict", "Strict licensing"),
         "healthcare": ("universal", "SNS — universal public"),
         "climate": ("active", "Solar expansion; 2045 net-zero"),
         "immigration": ("open", "Golden visa (revised); path to citizenship"),
         "nuclear_stance": ("non-nuclear", "NPT")},
        {"ES": 92, "BR": 92, "AO": 85, "MZ": 85, "CV": 92, "FR": 85, "DE": 82, "IT": 82, "GB": 82, "US": 82,
         "NL": 80, "BE": 78, "EU": 90, "UA": 75,
         "CN": 55, "RU": 25, "IR": 30, "KP": 12, "IL": 52}),
    "GR": _p("Greece", "GR", 37.98, 23.73, "Athens", "10.4M", "$230B", 30,
        "Parliamentary republic",
        [("President", "Katerina Sakellaropoulou"), ("Prime Minister", "Kyriakos Mitsotakis (ND)"), ("Foreign Minister", "Giorgos Gerapetritis")],
        ["UN", "NATO", "EU", "OECD", "WTO", "Eurozone", "Schengen", "Council of Europe"],
        {"abortion": ("legal", "Legal up to 12 weeks"),
         "lgbtq_rights": ("legal", "Same-sex marriage legalized 2024"),
         "death_penalty": ("abolished", "Abolished 1993/2004"),
         "gun_control": ("strict", "Strict licensing"),
         "healthcare": ("universal", "ESY — universal coverage"),
         "climate": ("mixed", "2050 net-zero; wildfire crises"),
         "immigration": ("mixed", "Frontline for Mediterranean crossings"),
         "nuclear_stance": ("non-nuclear", "NPT")},
        {"CY": 98, "IL": 82, "EG": 78, "FR": 85, "US": 78, "DE": 72, "IT": 85, "ES": 80, "BG": 78, "RO": 75,
         "AM": 82, "IN": 72, "UA": 72, "SA": 62, "AE": 70,
         "CN": 52, "RU": 30, "TR": 20, "AL": 38, "MK": 40, "IR": 30, "KP": 10}),
    "CZ": _p("Czech Republic", "CZ", 50.08, 14.44, "Prague", "10.5M", "$330B", 37,
        "Parliamentary republic",
        [("President", "Petr Pavel"), ("Prime Minister", "Petr Fiala (ODS)"), ("Foreign Minister", "Jan Lipavský")],
        ["UN", "NATO", "EU", "OECD", "WTO", "Visegrád 4", "Schengen"],
        {"abortion": ("legal", "Legal on request up to 12 weeks"),
         "lgbtq_rights": ("mixed", "Civil unions since 2006; no full marriage"),
         "death_penalty": ("abolished", "Abolished 1990"),
         "gun_control": ("moderate", "Licensing; relatively high ownership for EU"),
         "healthcare": ("universal", "Public insurance; high quality"),
         "climate": ("mixed", "Coal phase-out 2033"),
         "immigration": ("restrictive", "Strict; major Ukraine refugee host"),
         "nuclear_stance": ("non-nuclear", "NPT; nuclear energy expansion")},
        {"SK": 95, "PL": 90, "DE": 88, "AT": 85, "HU": 72, "US": 88, "GB": 85, "FR": 78, "UA": 92, "LT": 85,
         "LV": 85, "EE": 85, "NL": 82, "IT": 75, "CA": 82, "JP": 72,
         "CN": 28, "RU": 8, "IR": 20, "KP": 8, "IL": 75, "TR": 55}),
    "HU": _p("Hungary", "HU", 47.50, 19.04, "Budapest", "9.7M", "$210B", 53,
        "Parliamentary republic",
        [("President", "Tamás Sulyok"), ("Prime Minister", "Viktor Orbán (Fidesz)"), ("Foreign Minister", "Péter Szijjártó")],
        ["UN", "NATO", "EU", "OECD", "WTO", "Visegrád 4", "Schengen", "Council of Europe"],
        {"abortion": ("legal", "Legal but increasing restrictions"),
         "lgbtq_rights": ("restricted", "'Child protection law' 2021; constitutional ban on adoption"),
         "death_penalty": ("abolished", "Abolished 1990"),
         "gun_control": ("strict", "Strict licensing"),
         "healthcare": ("universal", "Public insurance; underfunded"),
         "climate": ("reluctant", "Russia gas dependency; EU pressure"),
         "immigration": ("closed", "Border fence; anti-migration stance"),
         "nuclear_stance": ("non-nuclear", "NPT; Russian-built Paks NPP expansion")},
        {"PL": 72, "SK": 85, "CZ": 72, "AT": 78, "HR": 72, "SI": 72, "RS": 82, "RO": 62, "DE": 55, "IT": 68,
         "TR": 85, "CN": 75, "RU": 68, "IL": 72, "US": 62, "GB": 55, "FR": 45, "UA": 45, "JP": 62,
         "IR": 45, "KP": 22}),
    "RO": _p("Romania", "RO", 44.43, 26.10, "Bucharest", "19M", "$370B", 47,
        "Semi-presidential republic",
        [("President", "Nicușor Dan"), ("Prime Minister", "Ilie Bolojan (PNL)"), ("Foreign Minister", "Oana Țoiu")],
        ["UN", "NATO", "EU", "OECD applicant", "WTO", "Council of Europe", "Schengen (air/sea)"],
        {"abortion": ("legal", "Legal on request up to 14 weeks"),
         "lgbtq_rights": ("restricted", "Constitutional marriage=man+woman; no civil unions"),
         "death_penalty": ("abolished", "Abolished 1990"),
         "gun_control": ("strict", "Strict licensing"),
         "healthcare": ("universal", "Public insurance; underfunded"),
         "climate": ("mixed", "Coal phase-out; EU funds"),
         "immigration": ("restrictive", "EU rules"),
         "nuclear_stance": ("non-nuclear", "NPT; civilian nuclear")},
        {"MD": 95, "US": 90, "GB": 85, "DE": 80, "FR": 82, "PL": 85, "BG": 85, "IT": 80, "ES": 75, "GR": 75,
         "UA": 82, "TR": 68, "HU": 60, "IL": 72, "JP": 72, "KR": 72,
         "CN": 32, "RU": 8, "IR": 20, "KP": 10}),
    "BG": _p("Bulgaria", "BG", 42.70, 23.32, "Sofia", "6.8M", "$100B", 62,
        "Parliamentary republic",
        [("President", "Rumen Radev"), ("Prime Minister", "Rosen Zhelyazkov (GERB)"), ("Foreign Minister", "Georg Georgiev")],
        ["UN", "NATO", "EU", "WTO", "Council of Europe", "Schengen (partial)"],
        {"abortion": ("legal", "Legal on request up to 12 weeks"),
         "lgbtq_rights": ("restricted", "No marriage or unions; constitutional ban"),
         "death_penalty": ("abolished", "Abolished 1998"),
         "gun_control": ("strict", "Strict licensing"),
         "healthcare": ("mixed", "Public-private; underfunded"),
         "climate": ("mixed", "Coal-dependent; EU pressure"),
         "immigration": ("restrictive", "EU border state"),
         "nuclear_stance": ("non-nuclear", "NPT; Russian-built Kozloduy NPP")},
        {"GR": 82, "RO": 82, "RS": 72, "TR": 62, "US": 75, "DE": 75, "FR": 72, "IT": 75, "PL": 72, "UA": 72,
         "MK": 38, "CN": 45, "RU": 35, "IR": 22, "KP": 10, "IL": 62}),
    "SK": _p("Slovakia", "SK", 48.15, 17.11, "Bratislava", "5.5M", "$130B", 68,
        "Parliamentary republic",
        [("President", "Peter Pellegrini"), ("Prime Minister", "Robert Fico (Smer)"), ("Foreign Minister", "Juraj Blanár")],
        ["UN", "NATO", "EU", "OECD", "WTO", "Visegrád 4", "Eurozone", "Schengen"],
        {"abortion": ("legal", "Legal up to 12 weeks"),
         "lgbtq_rights": ("restricted", "Constitutional marriage=man+woman; no civil unions"),
         "death_penalty": ("abolished", "Abolished 1990"),
         "gun_control": ("strict", "Strict licensing"),
         "healthcare": ("universal", "Public insurance"),
         "climate": ("mixed", "EU targets compliance"),
         "immigration": ("restrictive", "Refused EU migrant quotas"),
         "nuclear_stance": ("non-nuclear", "NPT; civilian nuclear")},
        {"CZ": 95, "PL": 85, "HU": 85, "AT": 85, "DE": 82, "UA": 48, "US": 55, "FR": 68, "IT": 72, "RO": 72,
         "RS": 70, "RU": 58, "CN": 58, "JP": 72, "KR": 68, "IL": 68,
         "IR": 30, "KP": 15}),
    "HR": _p("Croatia", "HR", 45.81, 15.98, "Zagreb", "3.9M", "$80B", 75,
        "Parliamentary republic",
        [("President", "Zoran Milanović"), ("Prime Minister", "Andrej Plenković (HDZ)"), ("Foreign Minister", "Gordan Grlić Radman")],
        ["UN", "NATO", "EU", "WTO", "Eurozone", "Schengen", "Council of Europe"],
        {"abortion": ("legal", "Legal up to 10 weeks; high conscientious objection"),
         "lgbtq_rights": ("mixed", "Civil unions since 2014; no full marriage"),
         "death_penalty": ("abolished", "Abolished 1990"),
         "gun_control": ("moderate", "Licensing; moderate"),
         "healthcare": ("universal", "HZZO — universal"),
         "climate": ("active", "EU compliance"),
         "immigration": ("mixed", "Balkan route; Schengen since 2023"),
         "nuclear_stance": ("non-nuclear", "NPT")},
        {"SI": 82, "HU": 78, "AT": 85, "IT": 82, "DE": 85, "FR": 82, "US": 80, "GB": 78, "UA": 82,
         "BA": 62, "RS": 55, "ME": 70, "MK": 72, "PL": 78,
         "CN": 48, "RU": 15, "IR": 22, "KP": 10, "IL": 65}),
    "SI": _p("Slovenia", "SI", 46.05, 14.51, "Ljubljana", "2.1M", "$65B", 82,
        "Parliamentary republic",
        [("President", "Nataša Pirc Musar"), ("Prime Minister", "Robert Golob"), ("Foreign Minister", "Tanja Fajon")],
        ["UN", "NATO", "EU", "OECD", "WTO", "Eurozone", "Schengen", "Council of Europe"],
        {"abortion": ("legal", "Legal up to 10 weeks; constitutional right"),
         "lgbtq_rights": ("legal", "Same-sex marriage since 2022"),
         "death_penalty": ("abolished", "Abolished 1989"),
         "gun_control": ("strict", "Strict licensing"),
         "healthcare": ("universal", "Public insurance"),
         "climate": ("active", "2050 net-zero target"),
         "immigration": ("mixed", "Schengen; Balkan route"),
         "nuclear_stance": ("non-nuclear", "NPT; Krško NPP")},
        {"HR": 82, "AT": 90, "IT": 85, "HU": 78, "DE": 85, "FR": 80, "US": 78, "GB": 78, "UA": 85,
         "PL": 78, "CZ": 82, "SK": 82, "RS": 52, "BA": 68,
         "CN": 45, "RU": 18, "IR": 25, "KP": 10, "IL": 60}),
    "LT": _p("Lithuania", "LT", 54.69, 25.28, "Vilnius", "2.8M", "$80B", 80,
        "Semi-presidential republic",
        [("President", "Gitanas Nausėda"), ("Prime Minister", "Inga Ruginienė"), ("Foreign Minister", "Kęstutis Budrys")],
        ["UN", "NATO", "EU", "OECD", "WTO", "Eurozone", "Schengen", "Nordic-Baltic 8"],
        {"abortion": ("legal", "Legal up to 12 weeks"),
         "lgbtq_rights": ("restricted", "No same-sex marriage/unions; debates ongoing"),
         "death_penalty": ("abolished", "Abolished 1998"),
         "gun_control": ("strict", "Strict; post-Ukraine expansion"),
         "healthcare": ("universal", "Public insurance"),
         "climate": ("active", "EU compliance"),
         "immigration": ("restrictive", "Belarus border wall"),
         "nuclear_stance": ("non-nuclear", "NPT")},
        {"LV": 95, "EE": 95, "PL": 95, "US": 92, "UA": 95, "GB": 92, "DE": 85, "FR": 80, "NO": 88, "SE": 88,
         "FI": 88, "DK": 88, "CA": 88, "NL": 85, "CZ": 85, "JP": 78, "KR": 75, "TW": 85,
         "CN": 10, "RU": 2, "BY": 5, "IR": 18, "KP": 5, "IL": 72}),
    "LV": _p("Latvia", "LV", 56.95, 24.11, "Riga", "1.9M", "$45B", 94,
        "Parliamentary republic",
        [("President", "Edgars Rinkēvičs"), ("Prime Minister", "Evika Siliņa"), ("Foreign Minister", "Baiba Braže")],
        ["UN", "NATO", "EU", "OECD", "WTO", "Eurozone", "Schengen", "Nordic-Baltic 8"],
        {"abortion": ("legal", "Legal up to 12 weeks"),
         "lgbtq_rights": ("mixed", "Civil unions since 2024; no marriage"),
         "death_penalty": ("abolished", "Abolished 2012"),
         "gun_control": ("strict", "Strict licensing"),
         "healthcare": ("universal", "Public insurance"),
         "climate": ("active", "EU compliance"),
         "immigration": ("restrictive", "Russia/Belarus border tightening"),
         "nuclear_stance": ("non-nuclear", "NPT")},
        {"LT": 95, "EE": 95, "PL": 90, "US": 92, "UA": 92, "GB": 90, "DE": 85, "FR": 80, "NO": 88, "SE": 90,
         "FI": 92, "DK": 88, "CA": 85, "NL": 82, "JP": 78, "TW": 80,
         "CN": 22, "RU": 3, "BY": 8, "IR": 18, "KP": 5, "IL": 62}),
    "EE": _p("Estonia", "EE", 59.44, 24.75, "Tallinn", "1.4M", "$40B", 105,
        "Parliamentary republic",
        [("President", "Alar Karis"), ("Prime Minister", "Kristen Michal (Reform)"), ("Foreign Minister", "Margus Tsahkna")],
        ["UN", "NATO", "EU", "OECD", "WTO", "Eurozone", "Schengen", "Nordic-Baltic 8"],
        {"abortion": ("legal", "Legal up to 11 weeks"),
         "lgbtq_rights": ("legal", "Same-sex marriage since 2024 (first Baltic)"),
         "death_penalty": ("abolished", "Abolished 1998"),
         "gun_control": ("moderate", "Licensing; civilian defense reserves"),
         "healthcare": ("universal", "Public insurance; digital health leader"),
         "climate": ("active", "2050 net-zero"),
         "immigration": ("restrictive", "Russia border closures"),
         "nuclear_stance": ("non-nuclear", "NPT")},
        {"FI": 95, "LV": 95, "LT": 95, "SE": 92, "DE": 88, "US": 92, "GB": 92, "UA": 92, "PL": 90, "NO": 92,
         "DK": 90, "FR": 82, "NL": 85, "CA": 85, "JP": 78, "KR": 75, "TW": 82,
         "CN": 25, "RU": 3, "BY": 8, "IR": 18, "KP": 5, "IL": 72}),
    "IS": _p("Iceland", "IS", 64.15, -21.94, "Reykjavik", "380k", "$30B", 141,
        "Parliamentary republic",
        [("President", "Halla Tómasdóttir"), ("Prime Minister", "Kristrún Frostadóttir (SDA)"), ("Foreign Minister", "Þorgerður Katrín Gunnarsdóttir")],
        ["UN", "NATO", "EFTA", "OECD", "WTO", "Schengen", "Nordic Council", "Arctic Council"],
        {"abortion": ("legal", "Legal up to 22 weeks (most liberal EU)"),
         "lgbtq_rights": ("legal", "Same-sex marriage since 2010"),
         "death_penalty": ("abolished", "Abolished 1928"),
         "gun_control": ("moderate", "Hunting tradition; no armed police historically"),
         "healthcare": ("universal", "Tax-funded; top-ranked"),
         "climate": ("active", "Geothermal/hydro powered; 2040 net-zero"),
         "immigration": ("mixed", "Open; labor shortage"),
         "nuclear_stance": ("non-nuclear", "NPT; no standing army")},
        {"NO": 92, "DK": 92, "SE": 90, "FI": 85, "US": 85, "GB": 85, "CA": 85, "DE": 80, "FR": 75, "NL": 78,
         "UA": 85, "JP": 72, "KR": 68,
         "CN": 35, "RU": 18, "IR": 20, "KP": 10, "IL": 50}),
    "RS": _p("Serbia", "RS", 44.79, 20.45, "Belgrade", "6.6M", "$75B", 56,
        "Parliamentary republic",
        [("President", "Aleksandar Vučić (SNS)"), ("Prime Minister", "Đuro Macut"), ("Foreign Minister", "Marko Đurić")],
        ["UN", "CEFTA", "Council of Europe", "EU candidate", "Non-Aligned observer"],
        {"abortion": ("legal", "Legal on request up to 10 weeks"),
         "lgbtq_rights": ("restricted", "Decriminalized; no marriage; pride attacks"),
         "death_penalty": ("abolished", "Abolished 2002"),
         "gun_control": ("moderate", "High ownership culture"),
         "healthcare": ("universal", "Public system"),
         "climate": ("mixed", "Coal-dependent; EU pressure"),
         "immigration": ("restrictive", "Balkan route transit"),
         "nuclear_stance": ("non-nuclear", "NPT")},
        {"RU": 85, "CN": 85, "HU": 85, "BY": 70, "GR": 85, "CY": 75, "MK": 72, "BA": 60, "ME": 62,
         "DE": 62, "FR": 60, "IT": 68, "US": 45, "GB": 50, "UA": 40,
         "XK": 5, "AL": 25, "TR": 58, "IR": 55, "KP": 40, "IL": 70}),
    "BA": _p("Bosnia & Herzegovina", "BA", 43.86, 18.41, "Sarajevo", "3.2M", "$27B", 101,
        "Federal parliamentary republic (3-member presidency)",
        [("Presidency (Bosniak)", "Denis Bećirović"), ("Presidency (Croat)", "Željko Komšić"), ("Presidency (Serb)", "Željka Cvijanović"), ("Chair, Council of Ministers", "Borjana Krišto")],
        ["UN", "CEFTA", "Council of Europe", "EU candidate"],
        {"abortion": ("legal", "Legal up to 10 weeks"),
         "lgbtq_rights": ("restricted", "Decriminalized; no marriage/unions"),
         "death_penalty": ("abolished", "Abolished 1997/2002"),
         "gun_control": ("mixed", "Post-war prevalence"),
         "healthcare": ("mixed", "Public but fragmented (entities)"),
         "climate": ("mixed", "Coal-dependent"),
         "immigration": ("restrictive", "Balkan route"),
         "nuclear_stance": ("non-nuclear", "NPT")},
        {"TR": 78, "DE": 72, "AT": 72, "HR": 62, "HU": 62, "US": 72, "GB": 65, "IT": 65,
         "RS": 60, "ME": 70, "MK": 68, "AL": 70, "XK": 55,
         "CN": 50, "RU": 45, "SA": 62, "IR": 32, "IL": 52}),
    "ME": _p("Montenegro", "ME", 42.44, 19.26, "Podgorica", "620k", "$7B", 124,
        "Parliamentary republic",
        [("President", "Jakov Milatović"), ("Prime Minister", "Milojko Spajić"), ("Foreign Minister", "Filip Ivanović")],
        ["UN", "NATO (2017)", "CEFTA", "Council of Europe", "EU candidate"],
        {"abortion": ("legal", "Legal up to 10 weeks"),
         "lgbtq_rights": ("mixed", "Civil partnerships 2020"),
         "death_penalty": ("abolished", "Abolished 2002"),
         "gun_control": ("moderate", "High ownership culture"),
         "healthcare": ("universal", "Public system"),
         "climate": ("mixed", "EU candidate compliance"),
         "immigration": ("restrictive", "Balkan route"),
         "nuclear_stance": ("non-nuclear", "NPT")},
        {"HR": 72, "IT": 82, "US": 80, "GB": 78, "DE": 82, "FR": 75, "TR": 75,
         "RS": 58, "BA": 72, "AL": 72, "MK": 72, "XK": 62,
         "CN": 52, "RU": 20, "IR": 28, "IL": 55}),
    "MK": _p("North Macedonia", "MK", 41.99, 21.43, "Skopje", "2M", "$15B", 83,
        "Parliamentary republic",
        [("President", "Gordana Siljanovska-Davkova"), ("Prime Minister", "Hristijan Mickoski (VMRO)"), ("Foreign Minister", "Timčo Mucunski")],
        ["UN", "NATO (2020)", "CEFTA", "Council of Europe", "EU candidate"],
        {"abortion": ("legal", "Legal up to 10 weeks"),
         "lgbtq_rights": ("restricted", "Decriminalized; no marriage"),
         "death_penalty": ("abolished", "Abolished 1991"),
         "gun_control": ("moderate", "Licensing"),
         "healthcare": ("universal", "Public insurance"),
         "climate": ("mixed", "Coal phase-out planned"),
         "immigration": ("restrictive", "Balkan route"),
         "nuclear_stance": ("non-nuclear", "NPT")},
        {"AL": 85, "XK": 72, "TR": 80, "US": 85, "GB": 78, "DE": 80, "FR": 72, "IT": 72, "BG": 38, "GR": 40,
         "RS": 72, "BA": 68, "ME": 72,
         "CN": 45, "RU": 18, "IR": 28, "IL": 55}),
    "AL": _p("Albania", "AL", 41.33, 19.82, "Tirana", "2.7M", "$25B", 84,
        "Parliamentary republic",
        [("President", "Bajram Begaj"), ("Prime Minister", "Edi Rama (PS)"), ("Foreign Minister", "Igli Hasani")],
        ["UN", "NATO (2009)", "CEFTA", "OIC", "Council of Europe", "EU candidate"],
        {"abortion": ("legal", "Legal up to 12 weeks"),
         "lgbtq_rights": ("restricted", "Decriminalized; no marriage"),
         "death_penalty": ("abolished", "Abolished 2000/2007"),
         "gun_control": ("moderate", "Licensing"),
         "healthcare": ("universal", "Public; mostly private"),
         "climate": ("mixed", "Hydro-dependent"),
         "immigration": ("transit", "Italy deportation deal"),
         "nuclear_stance": ("non-nuclear", "NPT")},
        {"XK": 95, "US": 92, "IT": 85, "GB": 85, "TR": 82, "DE": 80, "FR": 72, "MK": 85, "ME": 72,
         "BA": 70, "HR": 72, "SI": 72,
         "RS": 25, "CN": 48, "RU": 15, "IR": 28, "IL": 68}),
    "XK": _p("Kosovo", "XK", 42.67, 21.16, "Pristina", "1.8M", "$10B", 146,
        "Parliamentary republic",
        [("President", "Vjosa Osmani"), ("Prime Minister", "Albin Kurti (VV)"), ("Foreign Minister", "Donika Gërvalla-Schwarz")],
        ["CEFTA (observer)", "partial recognition", "Council of Europe applicant"],
        {"abortion": ("legal", "Legal up to 10 weeks"),
         "lgbtq_rights": ("restricted", "Constitutional equality; no marriage"),
         "death_penalty": ("abolished", "Abolished 2008 (constitution)"),
         "gun_control": ("moderate", "Licensing; post-war prevalence"),
         "healthcare": ("mixed", "Public under construction"),
         "climate": ("low", "Coal-dependent"),
         "immigration": ("restrictive", "Schengen visa-free 2024"),
         "nuclear_stance": ("non-nuclear", "Aspires to NPT")},
        {"AL": 98, "US": 95, "GB": 90, "DE": 85, "FR": 72, "IT": 75, "TR": 80, "NL": 78,
         "MK": 72, "ME": 62, "HR": 72, "SI": 72,
         "RS": 3, "RU": 5, "CN": 12, "IR": 22, "IL": 60, "BA": 55}),
    "MD": _p("Moldova", "MD", 47.01, 28.86, "Chișinău", "2.5M", "$16B", 130,
        "Parliamentary republic",
        [("President", "Maia Sandu"), ("Prime Minister", "Dorin Recean"), ("Foreign Minister", "Mihai Popșoi")],
        ["UN", "Council of Europe", "CIS (partial)", "EU candidate"],
        {"abortion": ("legal", "Legal up to 12 weeks"),
         "lgbtq_rights": ("restricted", "Decriminalized; no marriage"),
         "death_penalty": ("abolished", "Abolished 1995"),
         "gun_control": ("strict", "Strict licensing"),
         "healthcare": ("universal", "Public insurance; poor quality"),
         "climate": ("mixed", "EU alignment"),
         "immigration": ("restrictive", "Transnistria issue"),
         "nuclear_stance": ("non-nuclear", "NPT")},
        {"RO": 95, "UA": 92, "US": 90, "DE": 85, "FR": 80, "GB": 82, "PL": 85, "IT": 78, "EU": 92,
         "TR": 65, "BY": 20, "RU": 8, "CN": 30, "IR": 20, "IL": 55}),
    "BY": _p("Belarus", "BY", 53.90, 27.57, "Minsk", "9.2M", "$68B", 64,
        "Presidential republic (authoritarian)",
        [("President", "Alexander Lukashenko"), ("Prime Minister", "Alexander Turchin"), ("Foreign Minister", "Maxim Ryzhenkov")],
        ["UN", "CSTO", "EAEU", "CIS", "Union State (Russia)", "SCO (observer)"],
        {"abortion": ("legal", "Legal up to 12 weeks"),
         "lgbtq_rights": ("banned", "Decriminalized but hostile; 'LGBT propaganda' banned"),
         "death_penalty": ("active", "Only European country still using; firing squad"),
         "gun_control": ("strict", "Civilian ownership very restricted"),
         "healthcare": ("universal", "Soviet-era public system"),
         "climate": ("low", "No Paris commitments"),
         "immigration": ("restrictive", "Used migrants as weapon vs EU 2021"),
         "nuclear_stance": ("hosting", "Hosts Russian tactical nukes (2023)")},
        {"RU": 98, "CN": 82, "KP": 78, "IR": 72, "VE": 72, "SY": 75, "CU": 72,
         "KZ": 60, "AM": 55, "KG": 62, "TJ": 62,
         "US": 5, "UA": 5, "GB": 8, "DE": 15, "FR": 15, "PL": 5, "LT": 8, "LV": 8, "EE": 8, "IL": 25}),
})
# Europe batch 1 complete


# ═══════════════ EUROPE MICRO-STATES ═══════════════
COUNTRY_PROFILES.update({
    "LU": _p("Luxembourg", "LU", 49.61, 6.13, "Luxembourg City", "660K", "$87B", 150, "Constitutional monarchy",
        [("Grand Duke", "Henri"), ("Prime Minister", "Luc Frieden"), ("Foreign Minister", "Xavier Bettel")],
        ["UN", "NATO", "EU", "Eurozone", "Schengen", "Benelux", "OECD", "WTO", "Council of Europe"],
        {"abortion": ("legal", "Legal up to 12 weeks"),
         "lgbtq_rights": ("legal", "Marriage equality since 2015"),
         "death_penalty": ("abolished", "Abolished 1979"),
         "gun_control": ("strict", "Licensing required"),
         "healthcare": ("universal", "Multi-payer social insurance"),
         "climate": ("strong", "Net-zero by 2050"),
         "immigration": ("open", "47% foreign-born"),
         "nuclear_stance": ("none", "Under NATO umbrella")},
        {"BE": 96, "DE": 94, "FR": 94, "NL": 94, "US": 92, "GB": 90, "IT": 90, "ES": 90, "AT": 92,
         "RU": 15, "BY": 15, "KP": 10, "IR": 18}),
    "MT": _p("Malta", "MT", 35.90, 14.51, "Valletta", "535K", "$21B", 130, "Parliamentary republic",
        [("President", "Myriam Spiteri Debono"), ("Prime Minister", "Robert Abela"), ("Foreign Minister", "Ian Borg")],
        ["UN", "EU", "Eurozone", "Schengen", "Commonwealth", "Council of Europe", "OSCE"],
        {"abortion": ("restricted", "Banned except to save life (2023 reform)"),
         "lgbtq_rights": ("legal", "Top-ranked in Europe for LGBT rights"),
         "death_penalty": ("abolished", "Abolished 2000"),
         "gun_control": ("strict", "Licensing required"),
         "healthcare": ("universal", "Public system"),
         "climate": ("moderate", "EU targets"),
         "immigration": ("strict", "Key Mediterranean entry point"),
         "nuclear_stance": ("none", "Neutral; not in NATO")},
        {"IT": 94, "GB": 90, "FR": 88, "DE": 88, "ES": 85, "LY": 55,
         "RU": 25, "KP": 10, "IR": 22}),
    "CY": _p("Cyprus", "CY", 35.17, 33.36, "Nicosia", "1.2M", "$32B", 86, "Presidential republic",
        [("President", "Nikos Christodoulides"), ("Foreign Minister", "Constantinos Kombos")],
        ["UN", "EU", "Eurozone", "Commonwealth", "Council of Europe", "Non-Aligned Movement", "Francophonie (assoc.)"],
        {"abortion": ("legal", "Legal up to 12 weeks"),
         "lgbtq_rights": ("legal", "Civil unions; no marriage"),
         "death_penalty": ("abolished", "Abolished 2002"),
         "gun_control": ("moderate", "Licensing required"),
         "healthcare": ("universal", "GESY since 2019"),
         "climate": ("moderate", "EU targets"),
         "immigration": ("moderate", "Frontline state"),
         "nuclear_stance": ("none", "Not in NATO; hosts UK bases")},
        {"GR": 98, "IL": 80, "EG": 75, "FR": 88, "DE": 85, "GB": 80, "RU": 50,
         "TR": 5, "KP": 10, "IR": 25}),
    "IS": _p("Iceland", "IS", 64.13, -21.94, "Reykjavík", "383K", "$31B", 145, "Parliamentary republic",
        [("President", "Halla Tómasdóttir"), ("Prime Minister", "Kristrún Frostadóttir"), ("Foreign Minister", "Þorgerður Katrín Gunnarsdóttir")],
        ["UN", "NATO (no military)", "EEA", "EFTA", "Schengen", "Nordic Council", "Arctic Council", "OECD", "WTO", "Council of Europe"],
        {"abortion": ("legal", "Legal up to 22 weeks (2019)"),
         "lgbtq_rights": ("legal", "Marriage equality since 2010"),
         "death_penalty": ("abolished", "Abolished 1928"),
         "gun_control": ("strict", "Licensing, no handguns"),
         "healthcare": ("universal", "Public system"),
         "climate": ("strong", "Carbon neutral by 2040"),
         "immigration": ("moderate", "Points-based"),
         "nuclear_stance": ("none", "NATO; no military, no nukes on soil")},
        {"NO": 96, "DK": 96, "SE": 94, "FI": 92, "US": 88, "GB": 88, "DE": 85, "CA": 88,
         "RU": 25, "CN": 40, "KP": 10}),
    "MC": _p("Monaco", "MC", 43.73, 7.42, "Monaco", "39K", "$8.6B", 180, "Constitutional monarchy",
        [("Prince", "Albert II"), ("Minister of State", "Didier Guillaume")],
        ["UN", "Council of Europe", "OSCE", "Francophonie"],
        {"abortion": ("restricted", "Very restricted; rape/health only"),
         "lgbtq_rights": ("partial", "Civil unions 2020; no marriage"),
         "death_penalty": ("abolished", "Abolished 1962"),
         "gun_control": ("strict", "Licensing required"),
         "healthcare": ("universal", "Public insurance"),
         "climate": ("strong", "Carbon neutral goals"),
         "immigration": ("strict", "Highly controlled"),
         "nuclear_stance": ("none", "Defended by France")},
        {"FR": 98, "IT": 92, "US": 85, "GB": 85, "DE": 85,
         "RU": 25, "KP": 10, "IR": 20}),
    "LI": _p("Liechtenstein", "LI", 47.14, 9.52, "Vaduz", "40K", "$6.9B", 180, "Constitutional monarchy",
        [("Prince", "Hans-Adam II"), ("Prime Minister", "Brigitte Haas")],
        ["UN", "EEA", "EFTA", "Schengen", "Council of Europe", "OSCE", "WTO"],
        {"abortion": ("restricted", "Very restricted"),
         "lgbtq_rights": ("partial", "Marriage legalized 2024"),
         "death_penalty": ("abolished", "Abolished 1987"),
         "gun_control": ("strict", "Licensing required"),
         "healthcare": ("universal", "Mandatory insurance"),
         "climate": ("strong", "EEA targets"),
         "immigration": ("strict", "Highly controlled"),
         "nuclear_stance": ("none", "No military")},
        {"CH": 98, "AT": 96, "DE": 94, "FR": 88, "IT": 88, "US": 85,
         "RU": 20, "KP": 10}),
    "AD": _p("Andorra", "AD", 42.51, 1.52, "Andorra la Vella", "80K", "$3.4B", 180, "Parliamentary co-principality",
        [("Head of Govt", "Xavier Espot Zamora"), ("Co-Prince", "Joan-Enric Vives (Bishop)")],
        ["UN", "Council of Europe", "OSCE", "WTO (observer)", "Francophonie"],
        {"abortion": ("banned", "Fully banned—one of Europe's strictest"),
         "lgbtq_rights": ("legal", "Marriage equality 2023"),
         "death_penalty": ("abolished", "Abolished 1990"),
         "gun_control": ("strict", "Licensing required"),
         "healthcare": ("universal", "Public system"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("strict", "Tight controls"),
         "nuclear_stance": ("none", "No military")},
        {"ES": 96, "FR": 96, "PT": 88, "IT": 85,
         "RU": 20, "KP": 10}),
    "SM": _p("San Marino", "SM", 43.94, 12.45, "San Marino", "34K", "$1.9B", 180, "Parliamentary republic",
        [("Captains Regent", "Rotating (6-month)")],
        ["UN", "Council of Europe", "OSCE", "IMF"],
        {"abortion": ("legal", "Legal up to 12 weeks (2022)"),
         "lgbtq_rights": ("partial", "Civil unions 2018; no marriage"),
         "death_penalty": ("abolished", "Abolished 1865 (first in Europe)"),
         "gun_control": ("strict", "Licensing required"),
         "healthcare": ("universal", "Public system"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("strict", "Tight controls"),
         "nuclear_stance": ("none", "Ceremonial military")},
        {"IT": 98, "VA": 92, "FR": 85, "DE": 85,
         "RU": 25, "KP": 10}),
    "VA": _p("Vatican City", "VA", 41.90, 12.45, "Vatican City", "800", "N/A", 200, "Ecclesiastical absolute monarchy",
        [("Pope", "Leo XIV"), ("Secretary of State", "Pietro Parolin")],
        ["UN (observer)", "OSCE", "Holy See diplomatic corps"],
        {"abortion": ("banned", "Absolute doctrinal ban"),
         "lgbtq_rights": ("banned", "Catholic doctrinal opposition"),
         "death_penalty": ("abolished", "Abolished 1969"),
         "gun_control": ("strict", "Swiss Guard only"),
         "healthcare": ("universal", "For residents"),
         "climate": ("strong", "Laudato Si'"),
         "immigration": ("closed", "Citizenship functional only"),
         "nuclear_stance": ("none", "Anti-nuclear; TPNW signatory")},
        {"IT": 96, "US": 80, "FR": 85, "ES": 85, "PL": 88, "BR": 85, "PH": 88,
         "CN": 25, "KP": 10}),
})


# ═══════════════ AMERICAS ═══════════════
COUNTRY_PROFILES.update({
    "MX": _p("Mexico", "MX", 19.43, -99.13, "Mexico City", "129M", "$1.79T", 31, "Federal presidential republic",
        [("President", "Claudia Sheinbaum"), ("Foreign Secretary", "Juan Ramón de la Fuente")],
        ["UN", "OAS", "USMCA", "G20", "OECD", "WTO", "Pacific Alliance", "CELAC", "APEC", "CPTPP"],
        {"abortion": ("legal", "Decriminalized federally 2023"),
         "lgbtq_rights": ("legal", "Marriage equality nationwide 2022"),
         "death_penalty": ("abolished", "Abolished 2005"),
         "gun_control": ("strict", "One legal gun store"),
         "healthcare": ("universal", "IMSS/ISSSTE mixed"),
         "climate": ("moderate", "Paris signatory, Pemex expansion"),
         "immigration": ("transit", "Major transit country; US pressure"),
         "nuclear_stance": ("none", "TPNW party; Tlatelolco founder")},
        {"US": 75, "CA": 88, "ES": 88, "BR": 85, "AR": 82, "CL": 85, "CO": 88, "PE": 85, "DE": 85, "FR": 85,
         "RU": 35, "KP": 15, "IR": 30, "CN": 60}),
    "BR": _p("Brazil", "BR", -15.78, -47.93, "Brasília", "216M", "$2.17T", 10, "Federal presidential republic",
        [("President", "Luiz Inácio Lula da Silva"), ("Foreign Minister", "Mauro Vieira")],
        ["UN", "OAS", "Mercosur", "G20", "BRICS", "CELAC", "UNASUR", "WTO", "Amazon Cooperation Treaty"],
        {"abortion": ("restricted", "Rape/life/anencephaly only"),
         "lgbtq_rights": ("legal", "Marriage equality since 2013"),
         "death_penalty": ("abolished", "Abolished except wartime"),
         "gun_control": ("moderate", "Tightened 2023"),
         "healthcare": ("universal", "SUS public system"),
         "climate": ("moderate", "Lula re-engaging; Amazon focus"),
         "immigration": ("open", "Regional openness"),
         "nuclear_stance": ("none", "NPT; Tlatelolco; civilian nuclear")},
        {"AR": 85, "PT": 92, "CN": 82, "RU": 62, "IN": 78, "ZA": 80, "US": 65, "FR": 82, "DE": 82, "MX": 85,
         "IL": 40, "KP": 20, "IR": 55}),
    "AR": _p("Argentina", "AR", -34.60, -58.38, "Buenos Aires", "46M", "$641B", 26, "Federal presidential republic",
        [("President", "Javier Milei"), ("Foreign Minister", "Gerardo Werthein")],
        ["UN", "OAS", "Mercosur", "G20", "CELAC", "UNASUR", "WTO"],
        {"abortion": ("legal", "Legal up to 14 weeks (2020)"),
         "lgbtq_rights": ("legal", "Marriage equality 2010; leader on trans rights"),
         "death_penalty": ("abolished", "Abolished 1984"),
         "gun_control": ("moderate", "Licensing required"),
         "healthcare": ("universal", "Public + private mix"),
         "climate": ("weak", "Milei rollback"),
         "immigration": ("open", "Historically open"),
         "nuclear_stance": ("none", "Civilian nuclear; NPT")},
        {"BR": 85, "CL": 85, "UY": 90, "PY": 80, "US": 82, "IL": 85, "IT": 88, "ES": 88, "DE": 82,
         "GB": 35, "RU": 40, "CN": 50, "KP": 15, "IR": 25, "VE": 15}),
    "CL": _p("Chile", "CL", -33.45, -70.67, "Santiago", "19M", "$336B", 42, "Presidential republic",
        [("President", "Gabriel Boric"), ("Foreign Minister", "Alberto van Klaveren")],
        ["UN", "OAS", "Pacific Alliance", "CELAC", "OECD", "APEC", "CPTPP", "WTO", "UNASUR"],
        {"abortion": ("restricted", "Legal on 3 grounds (2017)"),
         "lgbtq_rights": ("legal", "Marriage equality 2022"),
         "death_penalty": ("abolished", "Abolished 2001"),
         "gun_control": ("strict", "Strict licensing"),
         "healthcare": ("mixed", "FONASA public + ISAPRE private"),
         "climate": ("strong", "Carbon neutral by 2050"),
         "immigration": ("moderate", "Venezuelan/Haitian inflow"),
         "nuclear_stance": ("none", "NPT; Tlatelolco; TPNW signatory")},
        {"AR": 85, "PE": 70, "BR": 85, "US": 85, "CN": 75, "ES": 90, "DE": 85, "FR": 85, "MX": 85,
         "RU": 30, "KP": 15, "IR": 25, "VE": 20}),
    "CO": _p("Colombia", "CO", 4.71, -74.07, "Bogotá", "52M", "$364B", 43, "Presidential republic",
        [("President", "Gustavo Petro"), ("Foreign Minister", "Luis Gilberto Murillo")],
        ["UN", "OAS", "Pacific Alliance", "CELAC", "OECD", "WTO", "NATO global partner", "Andean Community"],
        {"abortion": ("legal", "Legal up to 24 weeks (2022)"),
         "lgbtq_rights": ("legal", "Marriage equality 2016"),
         "death_penalty": ("abolished", "Abolished 1910"),
         "gun_control": ("strict", "Civilian carry suspended"),
         "healthcare": ("universal", "EPS mixed system"),
         "climate": ("moderate", "Petro ending oil exploration"),
         "immigration": ("open", "Hosts 2M+ Venezuelans"),
         "nuclear_stance": ("none", "NPT; Tlatelolco")},
        {"US": 85, "EC": 78, "PE": 85, "BR": 85, "ES": 90, "CL": 85, "MX": 85, "AR": 82,
         "VE": 15, "RU": 30, "KP": 10, "IR": 25, "NI": 40}),
    "PE": _p("Peru", "PE", -12.05, -77.04, "Lima", "34M", "$267B", 51, "Presidential republic",
        [("President", "Dina Boluarte"), ("Foreign Minister", "Elmer Schialer")],
        ["UN", "OAS", "Pacific Alliance", "CELAC", "APEC", "CPTPP", "Andean Community", "WTO"],
        {"abortion": ("restricted", "Only to save life"),
         "lgbtq_rights": ("minimal", "No civil unions"),
         "death_penalty": ("restricted", "Only wartime treason"),
         "gun_control": ("strict", "Strict licensing"),
         "healthcare": ("mixed", "SIS public + EsSalud"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("moderate", "Venezuelan influx"),
         "nuclear_stance": ("none", "NPT; Tlatelolco")},
        {"US": 80, "CL": 70, "EC": 72, "BR": 82, "CO": 85, "ES": 90, "CN": 75, "JP": 82, "MX": 80,
         "VE": 10, "RU": 35, "KP": 12, "IR": 25, "BO": 45}),
    "VE": _p("Venezuela", "VE", 10.48, -66.90, "Caracas", "28M", "$92B", 49, "Federal presidential republic (authoritarian)",
        [("President", "Nicolás Maduro"), ("Foreign Minister", "Yván Gil")],
        ["UN", "OAS (withdrawn)", "OPEC", "ALBA", "CELAC", "UNASUR", "Petrocaribe", "Non-Aligned Movement"],
        {"abortion": ("restricted", "Only to save life"),
         "lgbtq_rights": ("minimal", "No marriage or unions"),
         "death_penalty": ("abolished", "Abolished 1863"),
         "gun_control": ("strict", "Civilian sales banned 2012"),
         "healthcare": ("collapsed", "Public system collapsed"),
         "climate": ("low", "Petro-economy"),
         "immigration": ("emigration", "7.7M have fled"),
         "nuclear_stance": ("none", "NPT; Tlatelolco")},
        {"CU": 98, "RU": 90, "CN": 85, "IR": 88, "NI": 92, "BY": 72, "BO": 78, "KP": 75, "SY": 80, "TR": 60,
         "US": 2, "GB": 8, "CO": 15, "BR": 50, "AR": 15, "CL": 20, "IL": 5}),
    "EC": _p("Ecuador", "EC", -0.18, -78.47, "Quito", "18M", "$119B", 68, "Presidential republic",
        [("President", "Daniel Noboa"), ("Foreign Minister", "Gabriela Sommerfeld")],
        ["UN", "OAS", "CELAC", "Andean Community", "UNASUR", "WTO"],
        {"abortion": ("restricted", "Rape/health only (2021)"),
         "lgbtq_rights": ("legal", "Marriage equality 2019"),
         "death_penalty": ("abolished", "Abolished 1906"),
         "gun_control": ("moderate", "Noboa relaxed 2023"),
         "healthcare": ("universal", "IESS public"),
         "climate": ("moderate", "Yasuní referendum halted drilling"),
         "immigration": ("moderate", "Venezuelan transit"),
         "nuclear_stance": ("none", "NPT; Tlatelolco")},
        {"US": 82, "CO": 78, "PE": 72, "ES": 88, "BR": 80, "MX": 82, "CL": 80,
         "VE": 20, "RU": 30, "KP": 12, "IR": 20}),
    "BO": _p("Bolivia", "BO", -16.49, -68.15, "La Paz", "12M", "$45B", 80, "Presidential republic",
        [("President", "Luis Arce"), ("Foreign Minister", "Celinda Sosa Lunda")],
        ["UN", "OAS", "ALBA", "CELAC", "Andean Community", "Mercosur (acceding)", "UNASUR"],
        {"abortion": ("restricted", "Rape/health only"),
         "lgbtq_rights": ("partial", "Civil unions recognized 2023"),
         "death_penalty": ("abolished", "Abolished 1997"),
         "gun_control": ("strict", "Licensing required"),
         "healthcare": ("mixed", "SUS universal enacted 2019"),
         "climate": ("moderate", "Rights-of-nature law"),
         "immigration": ("open", "Regional openness"),
         "nuclear_stance": ("none", "NPT; Tlatelolco")},
        {"VE": 82, "CU": 82, "RU": 72, "CN": 75, "IR": 65, "NI": 75, "AR": 78, "BR": 78, "PE": 70,
         "US": 25, "IL": 15, "KP": 35}),
    "PY": _p("Paraguay", "PY", -25.26, -57.58, "Asunción", "6.8M", "$43B", 95, "Presidential republic",
        [("President", "Santiago Peña"), ("Foreign Minister", "Rubén Ramírez Lezcano")],
        ["UN", "OAS", "Mercosur", "CELAC", "UNASUR", "WTO"],
        {"abortion": ("restricted", "Only to save life"),
         "lgbtq_rights": ("minimal", "No civil unions; constitution bans marriage"),
         "death_penalty": ("abolished", "Abolished 1992"),
         "gun_control": ("moderate", "Licensing required"),
         "healthcare": ("mixed", "IPS + public"),
         "climate": ("moderate", "High deforestation concerns"),
         "immigration": ("open", "Regional openness"),
         "nuclear_stance": ("none", "NPT; Tlatelolco")},
        {"BR": 82, "AR": 80, "UY": 78, "US": 80, "TW": 90, "IL": 82, "ES": 85, "CL": 78,
         "CN": 25, "RU": 35, "KP": 15, "IR": 20, "VE": 20}),
    "UY": _p("Uruguay", "UY", -34.90, -56.19, "Montevideo", "3.4M", "$77B", 86, "Presidential republic",
        [("President", "Yamandú Orsi"), ("Foreign Minister", "Mario Lubetkin")],
        ["UN", "OAS", "Mercosur", "CELAC", "UNASUR", "WTO"],
        {"abortion": ("legal", "Legal up to 12 weeks (2012)"),
         "lgbtq_rights": ("legal", "Marriage equality 2013"),
         "death_penalty": ("abolished", "Abolished 1907"),
         "gun_control": ("moderate", "Licensing required"),
         "healthcare": ("universal", "Integrated national system"),
         "climate": ("strong", "98% renewable electricity"),
         "immigration": ("open", "Open policy"),
         "nuclear_stance": ("none", "NPT; Tlatelolco")},
        {"AR": 90, "BR": 88, "CL": 85, "ES": 90, "US": 82, "MX": 85, "DE": 85, "FR": 85,
         "RU": 40, "KP": 15, "IR": 25, "VE": 30}),
    "GY": _p("Guyana", "GY", 6.80, -58.15, "Georgetown", "820K", "$16B", 150, "Parliamentary republic",
        [("President", "Irfaan Ali"), ("Foreign Minister", "Hugh Todd")],
        ["UN", "OAS", "CARICOM", "Commonwealth", "CELAC", "UNASUR", "Non-Aligned Movement"],
        {"abortion": ("legal", "Legal up to 8 weeks (1995)"),
         "lgbtq_rights": ("minimal", "Same-sex acts criminalized (colonial law)"),
         "death_penalty": ("active", "Retained but no executions since 1997"),
         "gun_control": ("strict", "Licensing required"),
         "healthcare": ("universal", "Public system"),
         "climate": ("strong", "Forest-covered; now oil producer"),
         "immigration": ("moderate", "Venezuelan influx"),
         "nuclear_stance": ("none", "NPT; Tlatelolco")},
        {"US": 85, "GB": 88, "BR": 82, "IN": 82, "CA": 85, "CARICOM": 92, "FR": 80,
         "VE": 8, "RU": 30, "KP": 15, "IR": 25}),
    "SR": _p("Suriname", "SR", 5.87, -55.17, "Paramaribo", "630K", "$4.2B", 160, "Presidential republic",
        [("President", "Chandrikapersad Santokhi"), ("Foreign Minister", "Albert Ramdin")],
        ["UN", "OAS", "CARICOM", "CELAC", "UNASUR", "OIC", "Non-Aligned Movement"],
        {"abortion": ("restricted", "Highly restricted"),
         "lgbtq_rights": ("minimal", "No civil unions"),
         "death_penalty": ("abolished", "Abolished 2015"),
         "gun_control": ("strict", "Licensing required"),
         "healthcare": ("universal", "Public system"),
         "climate": ("strong", "93% forested; carbon-negative"),
         "immigration": ("open", "Regional openness"),
         "nuclear_stance": ("none", "NPT; Tlatelolco")},
        {"NL": 85, "BR": 80, "US": 75, "GY": 82, "IN": 75, "CN": 72, "CARICOM": 92,
         "RU": 35, "KP": 15, "IR": 25, "VE": 45}),
    "CU": _p("Cuba", "CU", 23.11, -82.37, "Havana", "11M", "$107B", 76, "One-party socialist republic",
        [("President", "Miguel Díaz-Canel"), ("Prime Minister", "Manuel Marrero Cruz"), ("Foreign Minister", "Bruno Rodríguez")],
        ["UN", "ALBA", "CELAC", "Non-Aligned Movement", "Petrocaribe", "G77"],
        {"abortion": ("legal", "Legal on request"),
         "lgbtq_rights": ("legal", "Marriage equality 2022"),
         "death_penalty": ("active", "Retained; no executions since 2003"),
         "gun_control": ("strict", "Civilian ownership heavily restricted"),
         "healthcare": ("universal", "Renowned public system"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("emigration", "Mass emigration crisis"),
         "nuclear_stance": ("none", "NPT; TPNW party")},
        {"VE": 98, "RU": 85, "CN": 85, "NI": 90, "IR": 78, "BO": 82, "KP": 75, "BY": 72, "SY": 75, "MX": 75,
         "US": 3, "IL": 8, "GB": 25, "UA": 20}),
    "DO": _p("Dominican Republic", "DO", 18.47, -69.90, "Santo Domingo", "11M", "$121B", 82, "Presidential republic",
        [("President", "Luis Abinader"), ("Foreign Minister", "Roberto Álvarez")],
        ["UN", "OAS", "CARICOM (obs.)", "CELAC", "DR-CAFTA", "WTO"],
        {"abortion": ("banned", "Total ban—no exceptions"),
         "lgbtq_rights": ("minimal", "No civil unions"),
         "death_penalty": ("abolished", "Abolished 1966"),
         "gun_control": ("moderate", "Licensing required"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("moderate", "Climate-vulnerable island"),
         "immigration": ("restrictive", "Hard line on Haitian migrants"),
         "nuclear_stance": ("none", "NPT; Tlatelolco")},
        {"US": 88, "ES": 92, "TW": 60, "CO": 78, "MX": 82, "PR": 90, "CHAP": 88,
         "HT": 15, "VE": 20, "RU": 30, "KP": 10, "IR": 20, "CU": 35}),
    "HT": _p("Haiti", "HT", 18.59, -72.31, "Port-au-Prince", "11M", "$25B", 157, "Presidential republic (crisis)",
        [("Council President", "Leslie Voltaire"), ("Prime Minister", "Alix Didier Fils-Aimé")],
        ["UN", "OAS", "CARICOM", "CELAC", "Francophonie"],
        {"abortion": ("banned", "Criminalized—decrim draft stalled"),
         "lgbtq_rights": ("minimal", "Same-sex marriage banned 2017"),
         "death_penalty": ("abolished", "Abolished 1987"),
         "gun_control": ("weak", "State collapse; gang control"),
         "healthcare": ("weak", "System near-collapse"),
         "climate": ("low", "Capacity limited"),
         "immigration": ("emigration", "Mass departures amid violence"),
         "nuclear_stance": ("none", "NPT; Tlatelolco")},
        {"US": 72, "FR": 78, "CA": 80, "CARICOM": 88, "BR": 72, "DO": 15,
         "RU": 25, "KP": 15, "IR": 20}),
    "JM": _p("Jamaica", "JM", 17.97, -76.79, "Kingston", "2.8M", "$18B", 137, "Parliamentary democracy",
        [("PM", "Andrew Holness"), ("Foreign Minister", "Kamina Johnson Smith")],
        ["UN", "OAS", "CARICOM", "Commonwealth", "CELAC", "Non-Aligned Movement"],
        {"abortion": ("restricted", "Only to save life"),
         "lgbtq_rights": ("minimal", "Same-sex acts criminalized"),
         "death_penalty": ("active", "Retained; no executions since 1988"),
         "gun_control": ("strict", "Licensing required"),
         "healthcare": ("universal", "Public system"),
         "climate": ("strong", "Paris leadership; vulnerable island"),
         "immigration": ("moderate", "Regional movement"),
         "nuclear_stance": ("none", "NPT; Tlatelolco; TPNW signatory")},
        {"US": 88, "GB": 90, "CA": 88, "CARICOM": 94, "BS": 88, "TT": 88, "CN": 72,
         "RU": 25, "KP": 15, "IR": 20, "VE": 40}),
    "TT": _p("Trinidad & Tobago", "TT", 10.65, -61.51, "Port of Spain", "1.4M", "$29B", 128, "Parliamentary republic",
        [("President", "Christine Kangaloo"), ("PM", "Keith Rowley")],
        ["UN", "OAS", "CARICOM", "Commonwealth", "CELAC", "Non-Aligned Movement"],
        {"abortion": ("restricted", "Only to save health"),
         "lgbtq_rights": ("partial", "Acts decriminalized 2018"),
         "death_penalty": ("active", "Retained for murder"),
         "gun_control": ("strict", "Licensing required"),
         "healthcare": ("universal", "Public system"),
         "climate": ("moderate", "Oil/gas economy"),
         "immigration": ("moderate", "Venezuelan influx"),
         "nuclear_stance": ("none", "NPT; Tlatelolco")},
        {"US": 85, "GB": 88, "CA": 85, "CARICOM": 94, "JM": 88, "IN": 82,
         "VE": 35, "RU": 25, "KP": 15, "IR": 20}),
    "BS": _p("Bahamas", "BS", 25.05, -77.35, "Nassau", "410K", "$14B", 170, "Parliamentary democracy",
        [("PM", "Philip Davis"), ("Foreign Minister", "Frederick Mitchell")],
        ["UN", "OAS", "CARICOM", "Commonwealth", "CELAC"],
        {"abortion": ("restricted", "Only to save life"),
         "lgbtq_rights": ("minimal", "Decrim but no unions"),
         "death_penalty": ("active", "Retained; none since 2000"),
         "gun_control": ("strict", "Licensing required"),
         "healthcare": ("universal", "Public system"),
         "climate": ("strong", "Highly climate-vulnerable"),
         "immigration": ("moderate", "Hard line on Haitian migrants"),
         "nuclear_stance": ("none", "NPT; Tlatelolco; TPNW party")},
        {"US": 92, "GB": 92, "CA": 88, "CARICOM": 94,
         "RU": 25, "KP": 10, "IR": 15, "CU": 50}),
    "PA": _p("Panama", "PA", 8.98, -79.52, "Panama City", "4.4M", "$83B", 104, "Presidential republic",
        [("President", "José Raúl Mulino"), ("Foreign Minister", "Javier Martínez-Acha")],
        ["UN", "OAS", "CELAC", "SICA", "WTO", "Non-Aligned Movement"],
        {"abortion": ("restricted", "Rape/life/health only"),
         "lgbtq_rights": ("minimal", "No civil unions"),
         "death_penalty": ("abolished", "Abolished 1922"),
         "gun_control": ("strict", "Moratorium since 2012"),
         "healthcare": ("mixed", "CSS + public mix"),
         "climate": ("moderate", "Canal climate risk"),
         "immigration": ("strict", "Darién Gap crisis"),
         "nuclear_stance": ("none", "NPT; Tlatelolco")},
        {"US": 88, "CO": 75, "ES": 90, "CR": 82, "MX": 82, "PE": 82, "CL": 80,
         "VE": 25, "RU": 25, "KP": 10, "IR": 20}),
    "CR": _p("Costa Rica", "CR", 9.93, -84.09, "San José", "5.2M", "$86B", 200, "Presidential republic",
        [("President", "Rodrigo Chaves Robles"), ("Foreign Minister", "Arnoldo André Tinoco")],
        ["UN", "OAS", "CELAC", "SICA", "OECD", "WTO"],
        {"abortion": ("restricted", "Only to save life"),
         "lgbtq_rights": ("legal", "Marriage equality 2020"),
         "death_penalty": ("abolished", "Abolished 1877"),
         "gun_control": ("strict", "Licensing required"),
         "healthcare": ("universal", "CCSS renowned"),
         "climate": ("strong", "99% renewable electricity"),
         "immigration": ("moderate", "Nicaraguan influx"),
         "nuclear_stance": ("none", "NPT; Tlatelolco; TPNW party; no military since 1948")},
        {"US": 85, "ES": 92, "MX": 85, "CO": 82, "PA": 82, "CA": 85, "DE": 85,
         "NI": 10, "VE": 15, "RU": 30, "KP": 10, "IR": 15}),
    "GT": _p("Guatemala", "GT", 14.63, -90.52, "Guatemala City", "18M", "$95B", 84, "Presidential republic",
        [("President", "Bernardo Arévalo"), ("Foreign Minister", "Carlos Ramiro Martínez")],
        ["UN", "OAS", "CELAC", "SICA", "DR-CAFTA", "WTO"],
        {"abortion": ("restricted", "Only to save life"),
         "lgbtq_rights": ("minimal", "No unions; congressional hostility"),
         "death_penalty": ("abolished", "De facto abolished"),
         "gun_control": ("moderate", "Licensing required"),
         "healthcare": ("weak", "Public system limited"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("emigration", "Major US-bound migration"),
         "nuclear_stance": ("none", "NPT; Tlatelolco")},
        {"US": 85, "MX": 80, "IL": 88, "TW": 88, "ES": 88, "CR": 75, "HN": 72, "SV": 75,
         "VE": 20, "RU": 25, "KP": 10, "IR": 15, "CN": 25}),
    "HN": _p("Honduras", "HN", 14.07, -87.19, "Tegucigalpa", "10M", "$34B", 115, "Presidential republic",
        [("President", "Xiomara Castro"), ("Foreign Minister", "Enrique Reina")],
        ["UN", "OAS", "CELAC", "SICA", "DR-CAFTA", "WTO"],
        {"abortion": ("banned", "Total ban"),
         "lgbtq_rights": ("minimal", "Same-sex marriage banned"),
         "death_penalty": ("abolished", "Abolished 1956"),
         "gun_control": ("moderate", "Licensing required"),
         "healthcare": ("weak", "Public system limited"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("emigration", "Major US-bound migration"),
         "nuclear_stance": ("none", "NPT; Tlatelolco; TPNW party")},
        {"US": 78, "MX": 78, "CN": 72, "ES": 88, "CU": 70, "VE": 70, "NI": 70, "CR": 72,
         "IL": 45, "RU": 35, "KP": 15, "IR": 25}),
    "SV": _p("El Salvador", "SV", 13.69, -89.22, "San Salvador", "6.3M", "$32B", 102, "Presidential republic",
        [("President", "Nayib Bukele"), ("Foreign Minister", "Alexandra Hill Tinoco")],
        ["UN", "OAS", "CELAC", "SICA", "DR-CAFTA", "WTO"],
        {"abortion": ("banned", "Total ban—women jailed"),
         "lgbtq_rights": ("minimal", "Marriage constitutionally banned"),
         "death_penalty": ("abolished", "Abolished peacetime 1983"),
         "gun_control": ("moderate", "Licensing required"),
         "healthcare": ("mixed", "Public + ISSS"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("emigration", "Diaspora economy"),
         "nuclear_stance": ("none", "NPT; Tlatelolco")},
        {"US": 82, "MX": 78, "GT": 75, "HN": 72, "ES": 85, "IL": 75, "AR": 75,
         "VE": 20, "RU": 30, "KP": 15, "IR": 15, "CU": 35}),
    "NI": _p("Nicaragua", "NI", 12.14, -86.27, "Managua", "6.9M", "$17B", 94, "Presidential republic (authoritarian)",
        [("Co-Presidents", "Daniel Ortega & Rosario Murillo"), ("Foreign Minister", "Valdrack Jaentschke")],
        ["UN", "OAS (withdrawing)", "ALBA", "CELAC", "SICA", "Non-Aligned Movement"],
        {"abortion": ("banned", "Total ban since 2006"),
         "lgbtq_rights": ("minimal", "No unions"),
         "death_penalty": ("abolished", "Abolished 1979"),
         "gun_control": ("strict", "Tight licensing"),
         "healthcare": ("universal", "Public system"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("emigration", "Crackdowns driving exodus"),
         "nuclear_stance": ("none", "NPT; Tlatelolco; TPNW party")},
        {"VE": 92, "CU": 92, "RU": 92, "CN": 88, "IR": 82, "BY": 78, "KP": 75, "BO": 75, "SY": 75,
         "US": 5, "IL": 10, "CO": 15, "CR": 15, "UA": 15}),
    "BZ": _p("Belize", "BZ", 17.50, -88.20, "Belmopan", "410K", "$3.3B", 180, "Parliamentary democracy",
        [("PM", "Johnny Briceño"), ("Foreign Minister", "Francis Fonseca")],
        ["UN", "OAS", "CARICOM", "SICA", "Commonwealth", "CELAC", "Non-Aligned Movement"],
        {"abortion": ("restricted", "Broad grounds, hard in practice"),
         "lgbtq_rights": ("partial", "Acts decriminalized 2016"),
         "death_penalty": ("active", "Retained; no executions since 1985"),
         "gun_control": ("strict", "Licensing required"),
         "healthcare": ("universal", "Public system"),
         "climate": ("strong", "Reef-nation, climate-vulnerable"),
         "immigration": ("moderate", "Central American migration"),
         "nuclear_stance": ("none", "NPT; Tlatelolco; TPNW party")},
        {"GB": 90, "US": 85, "CARICOM": 92, "MX": 80, "CA": 85, "JM": 88,
         "GT": 30, "RU": 25, "KP": 10, "IR": 15, "VE": 35}),
})
# Americas batch complete


# ═══════════════ ASIA ═══════════════
COUNTRY_PROFILES.update({
    "PK": _p("Pakistan", "PK", 33.69, 73.05, "Islamabad", "240M", "$375B", 9, "Federal parliamentary republic",
        [("President", "Asif Ali Zardari"), ("PM", "Shehbaz Sharif"), ("Foreign Minister", "Ishaq Dar"), ("Army Chief", "Gen. Asim Munir")],
        ["UN", "OIC", "SCO", "ECO", "SAARC", "Commonwealth", "Non-Aligned Movement", "G77", "CPEC (with China)"],
        {"abortion": ("restricted", "Only to save health"),
         "lgbtq_rights": ("banned", "Same-sex acts criminalized"),
         "death_penalty": ("active", "Actively executes"),
         "gun_control": ("moderate", "Licensing, widely circumvented"),
         "healthcare": ("weak", "Mixed public + private"),
         "climate": ("low", "Climate-vulnerable; 2022 floods"),
         "immigration": ("restrictive", "Deporting Afghans"),
         "nuclear_stance": ("armed", "~170 warheads; not in NPT")},
        {"CN": 95, "SA": 88, "TR": 85, "KW": 80, "QA": 80, "AE": 78, "AF": 40, "IR": 68, "RU": 65, "AZ": 78, "MY": 78,
         "IN": 8, "IL": 5, "US": 55, "GB": 55, "FR": 60, "BD": 60}),
    "BD": _p("Bangladesh", "BD", 23.81, 90.41, "Dhaka", "173M", "$460B", 37, "Parliamentary republic",
        [("President", "Mohammed Shahabuddin"), ("Interim Chief Adviser", "Muhammad Yunus"), ("Foreign Adviser", "Md Touhid Hossain")],
        ["UN", "OIC", "SAARC", "Commonwealth", "Non-Aligned Movement", "BIMSTEC", "D-8", "G77"],
        {"abortion": ("restricted", "Only to save life; 'menstrual regulation' tolerated"),
         "lgbtq_rights": ("banned", "Same-sex acts criminalized"),
         "death_penalty": ("active", "Actively executes"),
         "gun_control": ("strict", "Strict licensing"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("low", "Extreme vulnerability"),
         "immigration": ("hosting", "Hosts 1M+ Rohingya"),
         "nuclear_stance": ("none", "NPT; TPNW signatory")},
        {"IN": 60, "CN": 80, "JP": 85, "GB": 80, "US": 75, "SA": 78, "MY": 80, "TR": 75, "RU": 70, "PK": 60,
         "MM": 15, "IL": 10, "KP": 25}),
    "AF": _p("Afghanistan", "AF", 34.52, 69.18, "Kabul", "42M", "$14B", 115, "Islamic Emirate (Taliban)",
        [("Supreme Leader", "Hibatullah Akhundzada"), ("PM (acting)", "Hasan Akhund"), ("Foreign Minister (acting)", "Amir Khan Muttaqi")],
        ["UN (seat disputed)", "OIC (suspended)", "ECO", "SAARC"],
        {"abortion": ("banned", "Banned"),
         "lgbtq_rights": ("banned", "Punishable by death"),
         "death_penalty": ("active", "Public executions"),
         "gun_control": ("weak", "Widespread"),
         "healthcare": ("weak", "Collapsed"),
         "climate": ("low", "No capacity"),
         "immigration": ("emigration", "6M+ refugees abroad"),
         "nuclear_stance": ("none", "Non-nuclear")},
        {"PK": 50, "QA": 72, "CN": 62, "RU": 62, "IR": 55, "UZ": 55, "TM": 55,
         "US": 8, "IN": 15, "GB": 10, "IL": 5, "FR": 10, "DE": 10}),
    "NP": _p("Nepal", "NP", 27.72, 85.32, "Kathmandu", "30M", "$42B", 137, "Federal parliamentary republic",
        [("President", "Ram Chandra Poudel"), ("PM", "KP Sharma Oli"), ("Foreign Minister", "Arzu Rana Deuba")],
        ["UN", "SAARC", "BIMSTEC", "Non-Aligned Movement", "G77"],
        {"abortion": ("legal", "Legal on request up to 12 weeks"),
         "lgbtq_rights": ("partial", "Third gender recognized; unions 2023"),
         "death_penalty": ("abolished", "Abolished 1997"),
         "gun_control": ("strict", "Strict licensing"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("strong", "Himalayan climate leader"),
         "immigration": ("open", "Open border with India"),
         "nuclear_stance": ("none", "NPT; TPNW signatory")},
        {"IN": 72, "CN": 78, "GB": 85, "US": 80, "JP": 85, "BD": 75, "BT": 82,
         "PK": 55, "RU": 55, "KP": 35, "IL": 50}),
    "BT": _p("Bhutan", "BT", 27.47, 89.63, "Thimphu", "780K", "$3B", 174, "Constitutional monarchy",
        [("King", "Jigme Khesar Namgyel Wangchuck"), ("PM", "Tshering Tobgay")],
        ["UN", "SAARC", "BIMSTEC", "Non-Aligned Movement", "G77"],
        {"abortion": ("restricted", "Rape/health only"),
         "lgbtq_rights": ("partial", "Decriminalized 2021"),
         "death_penalty": ("abolished", "Abolished 2004"),
         "gun_control": ("strict", "Strict"),
         "healthcare": ("universal", "Free at point of care"),
         "climate": ("strong", "Carbon-negative country"),
         "immigration": ("strict", "Tight controls"),
         "nuclear_stance": ("none", "NPT; TPNW signatory")},
        {"IN": 94, "BD": 78, "NP": 78, "JP": 85, "TH": 80, "GB": 75, "US": 70,
         "CN": 25, "PK": 40, "KP": 20, "IL": 35}),
    "LK": _p("Sri Lanka", "LK", 6.93, 79.86, "Colombo/Sri Jayawardenepura", "22M", "$89B", 80, "Presidential republic",
        [("President", "Anura Kumara Dissanayake"), ("PM", "Harini Amarasuriya"), ("Foreign Minister", "Vijitha Herath")],
        ["UN", "SAARC", "Commonwealth", "BIMSTEC", "Non-Aligned Movement", "G77"],
        {"abortion": ("restricted", "Only to save life"),
         "lgbtq_rights": ("banned", "Colonial-era criminalization"),
         "death_penalty": ("active", "Retained; no executions since 1976"),
         "gun_control": ("strict", "Strict licensing"),
         "healthcare": ("universal", "Free public system"),
         "climate": ("moderate", "Climate-vulnerable island"),
         "immigration": ("moderate", "Managed"),
         "nuclear_stance": ("none", "NPT; TPNW signatory")},
        {"IN": 80, "CN": 82, "JP": 78, "GB": 75, "US": 72, "BD": 75, "PK": 72,
         "IL": 40, "RU": 55, "KP": 25}),
    "MV": _p("Maldives", "MV", 4.17, 73.51, "Malé", "520K", "$7B", 200, "Presidential republic",
        [("President", "Mohamed Muizzu"), ("Foreign Minister", "Abdulla Khaleel")],
        ["UN", "SAARC", "OIC", "Commonwealth", "Non-Aligned Movement", "G77", "AOSIS"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "Sharia-based criminalization"),
         "death_penalty": ("active", "Retained; no executions since 1953"),
         "gun_control": ("strict", "Civilian ownership banned"),
         "healthcare": ("universal", "Aasandha public"),
         "climate": ("strong", "Existential climate threat"),
         "immigration": ("strict", "Tight controls"),
         "nuclear_stance": ("none", "NPT; TPNW party")},
        {"CN": 85, "SA": 85, "PK": 78, "IN": 58, "LK": 80, "GB": 75, "TR": 80,
         "IL": 5, "US": 60, "RU": 45, "KP": 25}),
    "MM": _p("Myanmar", "MM", 16.87, 96.20, "Naypyidaw", "54M", "$59B", 35, "Military junta",
        [("SAC Chair", "Min Aung Hlaing"), ("Foreign Minister", "Than Swe")],
        ["UN", "ASEAN (partial suspension)", "Non-Aligned Movement", "BIMSTEC", "G77"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "Colonial-era criminalization"),
         "death_penalty": ("active", "Resumed 2022"),
         "gun_control": ("weak", "Civil war context"),
         "healthcare": ("weak", "Collapsed"),
         "climate": ("low", "No capacity"),
         "immigration": ("emigration", "Refugee-producing state"),
         "nuclear_stance": ("none", "NPT; TPNW signatory")},
        {"CN": 88, "RU": 88, "IN": 60, "TH": 60, "BY": 68, "KP": 68, "LA": 65,
         "US": 5, "GB": 10, "FR": 10, "DE": 10, "BD": 15, "IL": 30}),
    "TH": _p("Thailand", "TH", 13.75, 100.50, "Bangkok", "70M", "$548B", 25, "Constitutional monarchy",
        [("King", "Vajiralongkorn"), ("PM", "Paetongtarn Shinawatra"), ("Foreign Minister", "Maris Sangiampongsa")],
        ["UN", "ASEAN", "APEC", "WTO", "Non-Aligned Movement", "G77", "RCEP", "BIMSTEC"],
        {"abortion": ("legal", "Legal up to 20 weeks (2022)"),
         "lgbtq_rights": ("legal", "Marriage equality Jan 2025—first in SE Asia"),
         "death_penalty": ("active", "Retained"),
         "gun_control": ("moderate", "Licensing required"),
         "healthcare": ("universal", "UCS public"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("moderate", "Host to many Myanmar refugees"),
         "nuclear_stance": ("none", "NPT; TPNW party; SEANWFZ")},
        {"JP": 85, "US": 80, "CN": 78, "KR": 82, "AU": 82, "IN": 72, "SG": 88, "MY": 82, "VN": 78, "LA": 82,
         "MM": 40, "KP": 35, "RU": 55, "IL": 60}),
    "VN": _p("Vietnam", "VN", 21.03, 105.85, "Hanoi", "100M", "$430B", 22, "One-party socialist republic",
        [("President", "Lương Cường"), ("PM", "Phạm Minh Chính"), ("Foreign Minister", "Bùi Thanh Sơn"), ("CPV GS", "Tô Lâm")],
        ["UN", "ASEAN", "APEC", "WTO", "Non-Aligned Movement", "CPTPP", "RCEP", "G77"],
        {"abortion": ("legal", "Legal on request"),
         "lgbtq_rights": ("partial", "Not criminalized; no recognition"),
         "death_penalty": ("active", "Actively executes"),
         "gun_control": ("strict", "Civilian ownership banned"),
         "healthcare": ("universal", "Social health insurance"),
         "climate": ("moderate", "Net-zero by 2050 pledge"),
         "immigration": ("strict", "Tight controls"),
         "nuclear_stance": ("none", "NPT; TPNW party; SEANWFZ")},
        {"RU": 82, "CN": 55, "LA": 90, "KH": 78, "JP": 85, "KR": 85, "US": 75, "IN": 78, "FR": 78, "SG": 80,
         "TW": 40, "PH": 55, "KP": 40, "IL": 50}),
    "PH": _p("Philippines", "PH", 14.60, 120.98, "Manila", "117M", "$437B", 32, "Presidential republic",
        [("President", "Ferdinand Marcos Jr."), ("VP", "Sara Duterte"), ("Foreign Secretary", "Enrique Manalo")],
        ["UN", "ASEAN", "APEC", "WTO", "Non-Aligned Movement", "RCEP", "US-Philippines alliance (MDT)"],
        {"abortion": ("banned", "Total ban, criminal"),
         "lgbtq_rights": ("minimal", "No civil unions"),
         "death_penalty": ("abolished", "Abolished 2006"),
         "gun_control": ("moderate", "Licensing required"),
         "healthcare": ("universal", "UHC since 2019"),
         "climate": ("moderate", "Highly vulnerable; Paris signatory"),
         "immigration": ("moderate", "Diaspora-sending country"),
         "nuclear_stance": ("none", "NPT; TPNW party; SEANWFZ")},
        {"US": 90, "JP": 92, "AU": 88, "KR": 85, "IN": 78, "SG": 82, "VA": 88, "CA": 85, "GB": 82,
         "CN": 15, "RU": 30, "KP": 15, "IR": 30}),
    "ID": _p("Indonesia", "ID", -6.21, 106.85, "Jakarta", "278M", "$1.37T", 13, "Presidential republic",
        [("President", "Prabowo Subianto"), ("VP", "Gibran Rakabuming"), ("Foreign Minister", "Sugiono")],
        ["UN", "ASEAN", "OIC", "G20", "APEC", "WTO", "Non-Aligned Movement", "RCEP", "D-8", "BRICS (joining 2025)"],
        {"abortion": ("restricted", "Rape/health only"),
         "lgbtq_rights": ("minimal", "Not federally criminalized; Aceh bans"),
         "death_penalty": ("active", "Retained; conditional moratorium (2023 code)"),
         "gun_control": ("strict", "Civilian ownership banned"),
         "healthcare": ("universal", "JKN/BPJS"),
         "climate": ("moderate", "Major deforestation issues"),
         "immigration": ("moderate", "Regional managed"),
         "nuclear_stance": ("none", "NPT; TPNW party; SEANWFZ")},
        {"JP": 82, "AU": 80, "KR": 82, "MY": 85, "SG": 85, "IN": 78, "SA": 82, "US": 75, "CN": 72, "RU": 62,
         "IL": 10, "KP": 30, "TL": 70}),
    "MY": _p("Malaysia", "MY", 3.14, 101.69, "Kuala Lumpur", "34M", "$430B", 40, "Federal constitutional monarchy",
        [("King (YDPA)", "Sultan Ibrahim"), ("PM", "Anwar Ibrahim"), ("Foreign Minister", "Mohamad Hasan")],
        ["UN", "ASEAN", "OIC", "Commonwealth", "APEC", "WTO", "Non-Aligned Movement", "RCEP", "D-8", "CPTPP"],
        {"abortion": ("restricted", "Only to save health"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("active", "Mandatory death lifted 2023"),
         "gun_control": ("strict", "Tight licensing"),
         "healthcare": ("universal", "Public + private mix"),
         "climate": ("moderate", "Net-zero by 2050 pledge"),
         "immigration": ("moderate", "Labor migration"),
         "nuclear_stance": ("none", "NPT; TPNW party; SEANWFZ")},
        {"SG": 80, "ID": 85, "TH": 82, "VN": 75, "CN": 78, "SA": 85, "TR": 82, "JP": 82, "GB": 82, "AU": 80,
         "IL": 3, "KP": 25, "RU": 50}),
    "SG": _p("Singapore", "SG", 1.35, 103.82, "Singapore", "5.9M", "$501B", 60, "Parliamentary republic",
        [("President", "Tharman Shanmugaratnam"), ("PM", "Lawrence Wong"), ("Foreign Minister", "Vivian Balakrishnan")],
        ["UN", "ASEAN", "Commonwealth", "APEC", "WTO", "Non-Aligned Movement", "CPTPP", "RCEP"],
        {"abortion": ("legal", "Legal on request to 24 weeks"),
         "lgbtq_rights": ("partial", "Decriminalized 2022; no marriage"),
         "death_penalty": ("active", "Actively executes drug offenses"),
         "gun_control": ("strict", "Extremely tight"),
         "healthcare": ("universal", "Medisave/Medishield"),
         "climate": ("moderate", "Net-zero by 2050 pledge"),
         "immigration": ("moderate", "Points-based"),
         "nuclear_stance": ("none", "NPT; TPNW signatory; SEANWFZ")},
        {"US": 88, "JP": 90, "KR": 88, "AU": 88, "GB": 90, "IN": 85, "MY": 80, "ID": 85, "TH": 88, "VN": 80, "NZ": 88,
         "KP": 30, "RU": 50, "IR": 40}),
    "KH": _p("Cambodia", "KH", 11.56, 104.92, "Phnom Penh", "17M", "$32B", 106, "Constitutional monarchy",
        [("King", "Norodom Sihamoni"), ("PM", "Hun Manet"), ("Foreign Minister", "Sok Chenda Sophea")],
        ["UN", "ASEAN", "Non-Aligned Movement", "WTO", "Francophonie", "RCEP", "G77"],
        {"abortion": ("legal", "Legal up to 12 weeks"),
         "lgbtq_rights": ("minimal", "Not criminalized; no recognition"),
         "death_penalty": ("abolished", "Abolished 1989"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("low", "Limited capacity"),
         "immigration": ("moderate", "Regional labor movement"),
         "nuclear_stance": ("none", "NPT; TPNW party; SEANWFZ")},
        {"CN": 95, "LA": 80, "VN": 60, "TH": 55, "KR": 80, "JP": 85, "RU": 68, "FR": 75,
         "US": 40, "IL": 45, "KP": 40, "GB": 55}),
    "LA": _p("Laos", "LA", 17.97, 102.60, "Vientiane", "7.5M", "$15B", 140, "One-party socialist republic",
        [("President", "Thongloun Sisoulith"), ("PM", "Sonexay Siphandone"), ("Foreign Minister", "Thongsavanh Phomvihane")],
        ["UN", "ASEAN", "Non-Aligned Movement", "WTO", "Francophonie", "RCEP", "G77"],
        {"abortion": ("banned", "Except to save life"),
         "lgbtq_rights": ("minimal", "Not criminalized; no recognition"),
         "death_penalty": ("active", "Retained; no recent executions"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("low", "Limited capacity"),
         "immigration": ("strict", "Tight controls"),
         "nuclear_stance": ("none", "NPT; TPNW party; SEANWFZ")},
        {"VN": 92, "CN": 88, "TH": 78, "KH": 80, "RU": 72, "KP": 55, "MM": 65, "JP": 75,
         "US": 40, "IL": 35, "GB": 55, "TW": 15}),
    "BN": _p("Brunei", "BN", 4.90, 114.94, "Bandar Seri Begawan", "460K", "$17B", 170, "Absolute monarchy",
        [("Sultan", "Hassanal Bolkiah"), ("Foreign Minister", "Erywan Yusof")],
        ["UN", "ASEAN", "OIC", "Commonwealth", "APEC", "WTO", "CPTPP", "RCEP"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "Sharia penalties introduced 2019"),
         "death_penalty": ("active", "Retained; no executions since 1957"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Free public system"),
         "climate": ("moderate", "Oil economy"),
         "immigration": ("strict", "Tight controls"),
         "nuclear_stance": ("none", "NPT; SEANWFZ")},
        {"MY": 88, "SG": 80, "ID": 82, "SA": 85, "GB": 85, "JP": 82, "CN": 75, "TH": 80,
         "IL": 5, "KP": 25, "RU": 50}),
    "TL": _p("Timor-Leste", "TL", -8.56, 125.57, "Dili", "1.4M", "$2B", 180, "Semi-presidential republic",
        [("President", "José Ramos-Horta"), ("PM", "Xanana Gusmão"), ("Foreign Minister", "Bendito Freitas")],
        ["UN", "ASEAN (applicant)", "CPLP", "Non-Aligned Movement", "G7+", "WTO"],
        {"abortion": ("restricted", "Only to save life"),
         "lgbtq_rights": ("minimal", "Decriminalized; no recognition"),
         "death_penalty": ("abolished", "Abolished 1999"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Public system"),
         "climate": ("moderate", "Climate-vulnerable"),
         "immigration": ("moderate", "Managed"),
         "nuclear_stance": ("none", "TPNW party")},
        {"AU": 88, "PT": 90, "ID": 78, "JP": 85, "US": 82, "NZ": 85, "BR": 78,
         "CN": 45, "RU": 30, "KP": 15, "IL": 40}),
    "MN": _p("Mongolia", "MN", 47.89, 106.91, "Ulaanbaatar", "3.4M", "$19B", 98, "Parliamentary republic",
        [("President", "Ukhnaagiin Khürelsükh"), ("PM", "Luvsannamsrain Oyun-Erdene"), ("Foreign Minister", "Batmunkh Battsetseg")],
        ["UN", "Non-Aligned Movement", "WTO", "OSCE", "G77", "ASEAN Regional Forum"],
        {"abortion": ("legal", "Legal on request"),
         "lgbtq_rights": ("partial", "Decriminalized; hate-crime law"),
         "death_penalty": ("abolished", "Abolished 2017"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Public system"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("strict", "Tight controls"),
         "nuclear_stance": ("none", "NPT; TPNW party; nuclear-weapons-free status")},
        {"RU": 75, "CN": 72, "JP": 85, "KR": 85, "US": 80, "IN": 80, "DE": 82, "TR": 78,
         "KP": 35, "IR": 35, "IL": 55}),
    "TW": _p("Taiwan (ROC)", "TW", 25.03, 121.57, "Taipei", "23M", "$790B", 21, "Semi-presidential republic",
        [("President", "Lai Ching-te"), ("VP", "Hsiao Bi-khim"), ("Premier", "Cho Jung-tai"), ("Foreign Minister", "Lin Chia-lung")],
        ["UN (excluded)", "WTO", "APEC", "unofficial relations with most of the world"],
        {"abortion": ("legal", "Legal up to 24 weeks"),
         "lgbtq_rights": ("legal", "Marriage equality 2019—first in Asia"),
         "death_penalty": ("active", "Retained; ruled constitutional 2024"),
         "gun_control": ("strict", "Extremely tight"),
         "healthcare": ("universal", "NHI renowned single-payer"),
         "climate": ("moderate", "Net-zero by 2050 pledge"),
         "immigration": ("moderate", "New Southbound policy"),
         "nuclear_stance": ("none", "Nuclear weapons program abandoned 1988")},
        {"US": 95, "JP": 95, "PY": 90, "GT": 88, "VA": 88, "PH": 75, "LT": 92, "CZ": 90, "PL": 85, "GB": 88, "FR": 80, "CA": 88, "AU": 90, "KR": 80,
         "CN": 2, "KP": 5, "RU": 15, "IR": 15, "NI": 10}),
    "KZ": _p("Kazakhstan", "KZ", 51.16, 71.47, "Astana", "20M", "$261B", 66, "Presidential republic",
        [("President", "Kassym-Jomart Tokayev"), ("PM", "Olzhas Bektenov"), ("Foreign Minister", "Murat Nurtleu")],
        ["UN", "CSTO", "EAEU", "CIS", "SCO", "OSCE", "OIC", "WTO", "Turkic States Organization"],
        {"abortion": ("legal", "Legal on request"),
         "lgbtq_rights": ("minimal", "Decriminalized; no recognition; 'propaganda' bill"),
         "death_penalty": ("abolished", "Abolished 2021"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Public insurance"),
         "climate": ("moderate", "Carbon neutral by 2060 pledge"),
         "immigration": ("moderate", "Managed"),
         "nuclear_stance": ("none", "Voluntarily denuclearized 1995; CANWFZ")},
        {"RU": 75, "CN": 78, "TR": 85, "UZ": 85, "KG": 82, "AZ": 82, "BY": 68, "IN": 75, "DE": 82, "FR": 80, "US": 70,
         "UA": 50, "KP": 25, "IR": 55, "IL": 62}),
    "UZ": _p("Uzbekistan", "UZ", 41.31, 69.25, "Tashkent", "36M", "$101B", 62, "Presidential republic",
        [("President", "Shavkat Mirziyoyev"), ("PM", "Abdulla Aripov"), ("Foreign Minister", "Bakhtiyor Saidov")],
        ["UN", "CIS", "SCO", "OIC", "OSCE", "Turkic States Organization", "ECO", "G77"],
        {"abortion": ("legal", "Legal on request"),
         "lgbtq_rights": ("banned", "Same-sex acts criminalized"),
         "death_penalty": ("abolished", "Abolished 2008"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Public system"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("moderate", "Labor-exporting country"),
         "nuclear_stance": ("none", "NPT; CANWFZ")},
        {"RU": 72, "CN": 78, "KZ": 85, "TR": 85, "KG": 75, "TJ": 70, "TM": 65, "IN": 78, "DE": 78,
         "US": 65, "UA": 50, "KP": 25, "IL": 55}),
    "TM": _p("Turkmenistan", "TM", 37.95, 58.38, "Ashgabat", "6.5M", "$66B", 85, "Presidential republic (authoritarian)",
        [("President", "Serdar Berdimuhamedow"), ("National Leader", "Gurbanguly Berdimuhamedow"), ("Foreign Minister", "Rashid Meredov")],
        ["UN (permanent neutrality recognized 1995)", "CIS (associate)", "OIC", "ECO", "NAM"],
        {"abortion": ("legal", "Legal on request"),
         "lgbtq_rights": ("banned", "Same-sex acts criminalized"),
         "death_penalty": ("abolished", "Abolished 1999"),
         "gun_control": ("strict", "Civilian ownership banned"),
         "healthcare": ("universal", "Public system"),
         "climate": ("low", "Poor capacity"),
         "immigration": ("strict", "Highly restricted exit/entry"),
         "nuclear_stance": ("none", "NPT; CANWFZ; permanent neutrality")},
        {"RU": 72, "CN": 82, "TR": 85, "IR": 78, "UZ": 72, "KZ": 72, "AZ": 75,
         "US": 55, "UA": 45, "KP": 25, "IL": 45}),
    "TJ": _p("Tajikistan", "TJ", 38.54, 68.78, "Dushanbe", "10M", "$13B", 115, "Presidential republic",
        [("President", "Emomali Rahmon"), ("PM", "Kokhir Rasulzoda"), ("Foreign Minister", "Sirojiddin Muhriddin")],
        ["UN", "CSTO", "CIS", "SCO", "OIC", "OSCE", "ECO", "G77"],
        {"abortion": ("legal", "Legal on request"),
         "lgbtq_rights": ("minimal", "Decriminalized; 'propaganda' restrictions"),
         "death_penalty": ("abolished", "Moratorium since 2004"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Public system"),
         "climate": ("low", "Climate-vulnerable"),
         "immigration": ("emigration", "Labor migration to Russia"),
         "nuclear_stance": ("none", "NPT; CANWFZ")},
        {"RU": 88, "CN": 82, "IR": 72, "KZ": 78, "UZ": 70, "KG": 75, "BY": 65, "IN": 70,
         "US": 55, "UA": 40, "AF": 30, "IL": 40}),
    "KG": _p("Kyrgyzstan", "KG", 42.87, 74.60, "Bishkek", "7M", "$14B", 106, "Presidential republic",
        [("President", "Sadyr Japarov"), ("Cabinet Chair", "Akylbek Japarov"), ("Foreign Minister", "Jeenbek Kulubaev")],
        ["UN", "CSTO", "EAEU", "CIS", "SCO", "OSCE", "OIC", "Turkic States Organization", "WTO"],
        {"abortion": ("legal", "Legal on request"),
         "lgbtq_rights": ("minimal", "Decriminalized; 'propaganda' law 2023"),
         "death_penalty": ("abolished", "Abolished 2007"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Public system"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("emigration", "Labor migration"),
         "nuclear_stance": ("none", "NPT; CANWFZ")},
        {"RU": 82, "CN": 72, "KZ": 82, "UZ": 75, "TJ": 68, "TR": 80, "BY": 68, "IN": 72,
         "US": 60, "UA": 45, "KP": 30, "IL": 45}),
})
# Asia batch complete


# ═══════════════ MIDDLE EAST ═══════════════
COUNTRY_PROFILES.update({
    "IQ": _p("Iraq", "IQ", 33.31, 44.36, "Baghdad", "44M", "$264B", 45, "Federal parliamentary republic",
        [("President", "Abdul Latif Rashid"), ("PM", "Mohammed Shia' Al Sudani"), ("Foreign Minister", "Fuad Hussein")],
        ["UN", "Arab League", "OPEC", "OIC", "Non-Aligned Movement", "G77"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "Criminalized 2024 with 10-15 years"),
         "death_penalty": ("active", "Actively executes"),
         "gun_control": ("weak", "Militias; widespread"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("low", "Climate-vulnerable"),
         "immigration": ("moderate", "Refugee host and source"),
         "nuclear_stance": ("none", "NPT; no weapons since 1991")},
        {"IR": 75, "JO": 78, "TR": 65, "RU": 62, "CN": 70, "QA": 70, "KW": 65, "FR": 70, "DE": 68, "US": 55,
         "IL": 5, "KP": 30, "SA": 50}),
    "SY": _p("Syria", "SY", 33.51, 36.30, "Damascus", "23M", "$10B", 64, "Transitional government (post-Assad)",
        [("Interim President", "Ahmed al-Sharaa"), ("Interim PM", "Mohammed al-Bashir"), ("Foreign Minister", "Asaad al-Shaibani")],
        ["UN", "Arab League (reinstated 2023)", "OIC", "Non-Aligned Movement", "G77"],
        {"abortion": ("restricted", "Only to save life"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("active", "Retained"),
         "gun_control": ("weak", "Civil war remnants"),
         "healthcare": ("weak", "War-devastated"),
         "climate": ("low", "No capacity"),
         "immigration": ("emigration", "6M+ refugees"),
         "nuclear_stance": ("none", "NPT; secret program destroyed 2007")},
        {"TR": 80, "QA": 82, "SA": 72, "JO": 70, "UA": 55, "GB": 55, "US": 55, "FR": 58,
         "RU": 25, "IR": 28, "IL": 15, "CN": 45, "KP": 30}),
    "LB": _p("Lebanon", "LB", 33.89, 35.50, "Beirut", "5.3M", "$23B", 115, "Parliamentary republic",
        [("President", "Joseph Aoun"), ("PM", "Nawaf Salam"), ("Foreign Minister", "Youssef Rajji")],
        ["UN", "Arab League", "OIC", "Francophonie", "Non-Aligned Movement", "G77"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "Criminalized (Art. 534)"),
         "death_penalty": ("active", "Retained; no executions since 2004"),
         "gun_control": ("moderate", "Widely owned"),
         "healthcare": ("mixed", "Public + private; strained"),
         "climate": ("low", "Economic crisis"),
         "immigration": ("hosting", "Hosts 1.5M+ Syrian refugees"),
         "nuclear_stance": ("none", "NPT")},
        {"FR": 85, "SA": 72, "QA": 78, "EG": 75, "JO": 78, "US": 72, "CY": 80, "AE": 72, "IT": 80,
         "IL": 5, "SY": 30, "IR": 45, "RU": 50, "KP": 25}),
    "JO": _p("Jordan", "JO", 31.95, 35.93, "Amman", "11M", "$50B", 81, "Constitutional monarchy",
        [("King", "Abdullah II"), ("PM", "Jafar Hassan"), ("Foreign Minister", "Ayman Safadi")],
        ["UN", "Arab League", "OIC", "Non-Aligned Movement", "WTO", "NATO global partner"],
        {"abortion": ("restricted", "Rape/health only"),
         "lgbtq_rights": ("minimal", "Decriminalized; no recognition"),
         "death_penalty": ("active", "Retained"),
         "gun_control": ("moderate", "Licensing required"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("moderate", "Water-scarce"),
         "immigration": ("hosting", "Hosts 2M+ Palestinians, 650K Syrians"),
         "nuclear_stance": ("none", "NPT; no weapons program")},
        {"US": 88, "SA": 88, "EG": 85, "AE": 85, "GB": 85, "FR": 82, "DE": 82, "IQ": 78, "PS": 80, "KW": 82,
         "IL": 40, "IR": 15, "RU": 35, "SY": 45, "KP": 20}),
    "YE": _p("Yemen", "YE", 15.37, 44.19, "Sana'a (disputed) / Aden (PLC)", "34M", "$21B", 90, "Civil war",
        [("PLC Chair", "Rashad al-Alimi"), ("PM", "Ahmed Awad bin Mubarak"), ("Houthi Leader", "Abdul-Malik al-Houthi")],
        ["UN", "Arab League", "OIC", "Non-Aligned Movement", "G77"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "Death penalty possible"),
         "death_penalty": ("active", "Widely used"),
         "gun_control": ("weak", "Widespread"),
         "healthcare": ("weak", "War-collapsed"),
         "climate": ("low", "No capacity"),
         "immigration": ("emigration", "World's worst humanitarian crisis"),
         "nuclear_stance": ("none", "NPT")},
        {"IR": 78, "RU": 50, "CN": 55, "SA": 40, "AE": 35, "EG": 60, "TR": 60, "PS": 70, "OM": 70,
         "IL": 3, "US": 25, "GB": 30, "SD": 60}),
    "OM": _p("Oman", "OM", 23.58, 58.41, "Muscat", "4.8M", "$108B", 68, "Absolute monarchy",
        [("Sultan", "Haitham bin Tariq"), ("Foreign Minister", "Sayyid Badr Albusaidi")],
        ["UN", "GCC", "Arab League", "OIC", "Non-Aligned Movement", "WTO", "G77"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("active", "Retained; no recent executions"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Free public system"),
         "climate": ("moderate", "Net-zero by 2050 pledge"),
         "immigration": ("moderate", "Managed labor migration"),
         "nuclear_stance": ("none", "NPT")},
        {"GB": 88, "IN": 88, "SA": 75, "AE": 75, "IR": 65, "QA": 70, "US": 80, "KW": 75, "BH": 75,
         "IL": 30, "KP": 20, "RU": 50, "YE": 68}),
    "AE": _p("UAE", "AE", 24.45, 54.38, "Abu Dhabi", "9.9M", "$504B", 54, "Federal absolute monarchy",
        [("President", "Mohammed bin Zayed"), ("VP/PM", "Mohammed bin Rashid Al Maktoum"), ("Foreign Minister", "Abdullah bin Zayed")],
        ["UN", "GCC", "Arab League", "OPEC", "OIC", "BRICS (joined 2024)", "WTO", "Abraham Accords"],
        {"abortion": ("restricted", "Rape/health only"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("active", "Retained"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Public + private"),
         "climate": ("moderate", "Net-zero by 2050 pledge; COP28 hosted"),
         "immigration": ("open", "88% expats; points-based"),
         "nuclear_stance": ("none", "NPT; civilian nuclear (Barakah)")},
        {"SA": 92, "US": 90, "EG": 85, "FR": 88, "IN": 88, "GB": 88, "CN": 85, "RU": 72, "IL": 82, "JO": 85, "BH": 90, "KR": 85, "JP": 85,
         "IR": 35, "QA": 65, "YE": 40, "KP": 25}),
    "QA": _p("Qatar", "QA", 25.29, 51.53, "Doha", "3M", "$219B", 60, "Absolute monarchy",
        [("Emir", "Tamim bin Hamad Al Thani"), ("PM/FM", "Mohammed bin Abdulrahman Al Thani")],
        ["UN", "GCC", "Arab League", "OPEC (left 2019)", "OIC", "Non-Aligned Movement", "WTO", "NATO major non-member ally"],
        {"abortion": ("restricted", "Rape/health only"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("active", "Retained; rare"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Free public system"),
         "climate": ("moderate", "Net-zero by 2050 pledge"),
         "immigration": ("open", "86% expats"),
         "nuclear_stance": ("none", "NPT")},
        {"TR": 92, "US": 88, "IR": 78, "PK": 82, "OM": 80, "FR": 85, "GB": 85, "DE": 82, "KW": 78, "CN": 80, "IQ": 70, "PS": 82,
         "IL": 35, "BH": 40, "SY": 60, "KP": 25}),
    "BH": _p("Bahrain", "BH", 26.23, 50.59, "Manama", "1.5M", "$44B", 94, "Constitutional monarchy",
        [("King", "Hamad bin Isa Al Khalifa"), ("PM", "Salman bin Hamad"), ("Foreign Minister", "Abdullatif bin Rashid Al Zayani")],
        ["UN", "GCC", "Arab League", "OIC", "Non-Aligned Movement", "WTO", "Abraham Accords"],
        {"abortion": ("restricted", "Rape/health only"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("active", "Resumed 2017"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Free public"),
         "climate": ("moderate", "Net-zero by 2060 pledge"),
         "immigration": ("open", "55% expats"),
         "nuclear_stance": ("none", "NPT")},
        {"SA": 96, "AE": 92, "US": 90, "EG": 85, "GB": 90, "KW": 88, "IL": 78, "FR": 82, "IN": 82, "JO": 85,
         "IR": 10, "QA": 40, "KP": 20}),
    "KW": _p("Kuwait", "KW", 29.38, 47.99, "Kuwait City", "4.3M", "$184B", 89, "Constitutional monarchy",
        [("Emir", "Mishal Al-Ahmad Al-Sabah"), ("PM", "Ahmad Abdullah Al-Ahmad Al-Sabah"), ("Foreign Minister", "Abdullah Ali Al-Yahya")],
        ["UN", "GCC", "Arab League", "OPEC", "OIC", "Non-Aligned Movement", "WTO", "NATO major non-member ally"],
        {"abortion": ("restricted", "Only to save health"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("active", "Resumed 2013"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Free public"),
         "climate": ("moderate", "Net-zero by 2050 pledge"),
         "immigration": ("moderate", "Kafala reforms ongoing"),
         "nuclear_stance": ("none", "NPT")},
        {"US": 92, "SA": 88, "AE": 85, "EG": 85, "GB": 88, "FR": 85, "DE": 82, "JO": 85, "BH": 88, "JP": 88,
         "IL": 5, "IR": 30, "IQ": 45, "KP": 25}),
    "PS": _p("Palestine", "PS", 31.95, 35.23, "Ramallah (PA) / Gaza (Hamas)", "5.5M", "$20B", 150, "Semi-presidential (disputed)",
        [("PA President", "Mahmoud Abbas"), ("PA PM", "Mohammad Mustafa"), ("Foreign Minister", "Varsen Aghabekian Shahin")],
        ["UN (observer state)", "Arab League", "OIC", "Non-Aligned Movement", "G77"],
        {"abortion": ("banned", "Except to save life"),
         "lgbtq_rights": ("banned", "Criminalized in Gaza; unclear WB"),
         "death_penalty": ("active", "Retained"),
         "gun_control": ("weak", "Conflict zone"),
         "healthcare": ("weak", "Devastated in Gaza"),
         "climate": ("low", "No capacity"),
         "immigration": ("refugee", "5.9M UNRWA refugees globally"),
         "nuclear_stance": ("none", "Supports TPNW")},
        {"TR": 90, "QA": 95, "IR": 85, "JO": 82, "EG": 80, "SA": 78, "ZA": 85, "MY": 85, "PK": 88, "YE": 78, "LB": 82,
         "IL": 1, "US": 25, "GB": 40, "DE": 50, "HU": 35}),
})
# Middle East batch complete


# ═══════════════ AFRICA ═══════════════
COUNTRY_PROFILES.update({
    "EG": _p("Egypt", "EG", 30.04, 31.24, "Cairo", "111M", "$398B", 15, "Presidential republic",
        [("President", "Abdel Fattah el-Sisi"), ("PM", "Mostafa Madbouly"), ("Foreign Minister", "Badr Abdelatty")],
        ["UN", "AU", "Arab League", "OIC", "Non-Aligned Movement", "BRICS (joined 2024)", "COMESA", "G77", "NATO MediDialog"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "De facto via debauchery laws"),
         "death_penalty": ("active", "Among top executors globally"),
         "gun_control": ("strict", "Tight licensing"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("moderate", "COP27 host 2022"),
         "immigration": ("hosting", "Hosts 500K+ Sudanese refugees"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"SA": 90, "AE": 88, "US": 82, "CN": 82, "RU": 75, "FR": 85, "DE": 82, "IT": 82, "JO": 85, "SD": 72, "GR": 82, "CY": 80, "BH": 85,
         "IL": 55, "TR": 45, "ET": 25, "QA": 55, "IR": 40, "KP": 20}),
    "DZ": _p("Algeria", "DZ", 36.75, 3.06, "Algiers", "46M", "$239B", 27, "Presidential republic",
        [("President", "Abdelmadjid Tebboune"), ("PM", "Nadir Larbaoui"), ("Foreign Minister", "Ahmed Attaf")],
        ["UN", "AU", "Arab League", "OIC", "OPEC", "Non-Aligned Movement", "G77", "Maghreb Union"],
        {"abortion": ("restricted", "Only to save health"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("active", "Retained; no executions since 1993"),
         "gun_control": ("strict", "Very tight"),
         "healthcare": ("universal", "Free public"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("moderate", "Sub-Saharan transit"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"RU": 85, "CN": 82, "FR": 55, "TN": 82, "TR": 78, "IT": 80, "DE": 78, "ES": 75, "PS": 88, "VE": 75,
         "IL": 3, "MA": 10, "US": 55, "UA": 40, "KP": 30}),
    "MA": _p("Morocco", "MA", 34.02, -6.84, "Rabat", "37M", "$148B", 58, "Constitutional monarchy",
        [("King", "Mohammed VI"), ("PM", "Aziz Akhannouch"), ("Foreign Minister", "Nasser Bourita")],
        ["UN", "AU (re-joined 2017)", "Arab League", "OIC", "Non-Aligned Movement", "Abraham Accords", "G77", "Maghreb Union", "Francophonie"],
        {"abortion": ("restricted", "Only to save health"),
         "lgbtq_rights": ("banned", "Criminalized (Art. 489)"),
         "death_penalty": ("active", "Retained; no executions since 1993"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "AMO + public"),
         "climate": ("strong", "Leading renewable push"),
         "immigration": ("moderate", "Sub-Saharan transit"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"FR": 88, "US": 88, "SA": 85, "AE": 85, "ES": 82, "IL": 78, "EG": 82, "JO": 85, "SN": 82, "DE": 80, "GB": 82,
         "DZ": 10, "IR": 15, "KP": 20, "VE": 35}),
    "TN": _p("Tunisia", "TN", 36.81, 10.18, "Tunis", "12M", "$52B", 78, "Presidential republic",
        [("President", "Kais Saied"), ("PM", "Kamel Madouri"), ("Foreign Minister", "Mohamed Ali Nafti")],
        ["UN", "AU", "Arab League", "OIC", "Non-Aligned Movement", "Francophonie", "Maghreb Union", "G77"],
        {"abortion": ("legal", "Legal on request (1973)"),
         "lgbtq_rights": ("banned", "Criminalized (Art. 230)"),
         "death_penalty": ("active", "Retained; no executions since 1991"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Public system"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("moderate", "Key Mediterranean transit"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"DZ": 85, "FR": 75, "IT": 82, "SA": 78, "TR": 78, "EG": 78, "DE": 78, "LY": 62, "QA": 72, "PS": 82,
         "IL": 10, "KP": 25, "MA": 55, "US": 65}),
    "LY": _p("Libya", "LY", 32.89, 13.19, "Tripoli (GNU) / Benghazi (HoR)", "7M", "$52B", 69, "Civil war / divided",
        [("GNU PM", "Abdul Hamid Dbeibeh"), ("HoR speaker", "Aguila Saleh"), ("Haftar (LNA)", "Khalifa Haftar")],
        ["UN", "AU", "Arab League", "OPEC", "OIC", "Maghreb Union", "G77"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("active", "Retained"),
         "gun_control": ("weak", "Militias; widespread"),
         "healthcare": ("weak", "War-damaged"),
         "climate": ("low", "No capacity"),
         "immigration": ("transit", "Major EU transit route"),
         "nuclear_stance": ("none", "NPT; program dismantled 2003")},
        {"TR": 72, "IT": 70, "EG": 65, "RU": 65, "AE": 60, "QA": 70, "SA": 65, "FR": 55,
         "IL": 10, "US": 50, "GR": 55, "KP": 25}),
    "SD": _p("Sudan", "SD", 15.50, 32.56, "Khartoum", "48M", "$109B", 70, "Civil war (military/RSF)",
        [("TSC Chair", "Abdel Fattah al-Burhan"), ("RSF leader", "Mohamed Hamdan Dagalo (Hemedti)")],
        ["UN", "AU", "Arab League", "OIC", "IGAD", "COMESA", "G77"],
        {"abortion": ("restricted", "Rape/life only"),
         "lgbtq_rights": ("banned", "Criminalized; death penalty removed 2020"),
         "death_penalty": ("active", "Retained"),
         "gun_control": ("weak", "Conflict zone"),
         "healthcare": ("weak", "Collapsed"),
         "climate": ("low", "No capacity"),
         "immigration": ("emigration", "10M displaced"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"EG": 82, "SA": 78, "UAE": 75, "RU": 70, "CN": 78, "TR": 72, "IR": 62,
         "IL": 25, "US": 35, "ET": 15, "SS": 30, "KP": 30}),
    "SS": _p("South Sudan", "SS", 4.85, 31.58, "Juba", "11M", "$5B", 140, "Presidential republic (fragile)",
        [("President", "Salva Kiir"), ("First VP", "Riek Machar"), ("Foreign Minister", "Ramadan Mohamed Goc")],
        ["UN", "AU", "IGAD", "EAC (joined 2016)", "COMESA", "Non-Aligned Movement", "G77"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("active", "Retained"),
         "gun_control": ("weak", "Conflict"),
         "healthcare": ("weak", "Fragile"),
         "climate": ("low", "No capacity"),
         "immigration": ("emigration", "2M+ refugees"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"KE": 82, "UG": 85, "US": 78, "EG": 75, "ET": 60, "TZ": 72, "CN": 72,
         "SD": 25, "RU": 55, "IL": 45, "KP": 20}),
    "ET": _p("Ethiopia", "ET", 9.02, 38.75, "Addis Ababa", "120M", "$156B", 49, "Federal parliamentary republic",
        [("President", "Taye Atske Selassie"), ("PM", "Abiy Ahmed"), ("Foreign Minister", "Gedion Timothewos")],
        ["UN", "AU (HQ)", "IGAD", "COMESA", "BRICS (joined 2024)", "Non-Aligned Movement", "G77"],
        {"abortion": ("legal", "Legal on broad grounds since 2005"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("active", "Retained; rare use"),
         "gun_control": ("moderate", "Licensing"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("strong", "Major reforestation"),
         "immigration": ("hosting", "800K+ refugees"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"CN": 88, "RU": 75, "UAE": 78, "SA": 72, "DJ": 82, "KE": 75, "IN": 78, "IT": 75, "US": 55, "TR": 78,
         "ER": 15, "EG": 25, "SS": 60, "SD": 30, "IL": 60}),
    "ER": _p("Eritrea", "ER", 15.34, 38.93, "Asmara", "3.6M", "$2B", 115, "One-party presidential republic",
        [("President", "Isaias Afwerki"), ("Foreign Minister", "Osman Saleh Mohammed")],
        ["UN", "AU", "COMESA (suspended)", "Non-Aligned Movement", "G77"],
        {"abortion": ("restricted", "Rape/health only"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("active", "Retained"),
         "gun_control": ("strict", "State-controlled"),
         "healthcare": ("universal", "Basic public"),
         "climate": ("low", "No capacity"),
         "immigration": ("emigration", "Mass outflow from conscription"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"RU": 82, "CN": 78, "IR": 62, "AE": 65, "EG": 68, "SD": 62, "SA": 60,
         "ET": 15, "US": 25, "GB": 30, "IL": 35, "KP": 55}),
    "DJ": _p("Djibouti", "DJ", 11.82, 42.59, "Djibouti", "1.1M", "$4B", 160, "Presidential republic",
        [("President", "Ismaïl Omar Guelleh"), ("PM", "Abdoulkader Kamil Mohamed"), ("Foreign Minister", "Mahmoud Ali Youssouf")],
        ["UN", "AU", "Arab League", "OIC", "IGAD", "COMESA", "Francophonie", "Non-Aligned Movement", "G77"],
        {"abortion": ("restricted", "Only to save health"),
         "lgbtq_rights": ("minimal", "Not criminalized; no recognition"),
         "death_penalty": ("abolished", "Abolished 1995"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("low", "Climate-vulnerable"),
         "immigration": ("hosting", "Hosts regional refugees"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"FR": 90, "US": 88, "CN": 85, "ET": 88, "JP": 88, "SA": 82, "IT": 85, "AE": 80, "IN": 82,
         "ER": 25, "KP": 25, "IR": 35, "RU": 55}),
    "SO": _p("Somalia", "SO", 2.04, 45.34, "Mogadishu", "17M", "$11B", 130, "Federal parliamentary republic",
        [("President", "Hassan Sheikh Mohamud"), ("PM", "Hamza Abdi Barre"), ("Foreign Minister", "Ahmed Moallim Fiqi")],
        ["UN", "AU", "Arab League", "OIC", "IGAD", "EAC (joined 2024)", "Non-Aligned Movement", "G77"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "Criminalized; al-Shabaab death penalty"),
         "death_penalty": ("active", "Retained"),
         "gun_control": ("weak", "Conflict zone"),
         "healthcare": ("weak", "Fragile"),
         "climate": ("low", "Extreme vulnerability"),
         "immigration": ("emigration", "Large diaspora"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"TR": 90, "QA": 82, "UAE": 70, "EG": 78, "SA": 75, "US": 75, "ET": 55, "KE": 65, "DJ": 78,
         "IL": 30, "KP": 20, "IR": 35, "RU": 45}),
    "KE": _p("Kenya", "KE", -1.29, 36.82, "Nairobi", "55M", "$118B", 59, "Presidential republic",
        [("President", "William Ruto"), ("Deputy President", "Kithure Kindiki"), ("Foreign Secretary", "Musalia Mudavadi")],
        ["UN", "AU", "EAC", "COMESA", "IGAD", "Commonwealth", "Non-Aligned Movement", "G77", "NATO global partner (candidate)"],
        {"abortion": ("restricted", "Only to save health"),
         "lgbtq_rights": ("banned", "Criminalized (colonial law)"),
         "death_penalty": ("active", "Retained; no executions since 1987"),
         "gun_control": ("strict", "Tight licensing"),
         "healthcare": ("universal", "SHA/NHIF"),
         "climate": ("strong", "Geothermal leader"),
         "immigration": ("hosting", "Hosts 700K+ refugees"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"US": 85, "GB": 82, "CN": 78, "JP": 82, "IN": 80, "TZ": 82, "UG": 82, "RW": 82, "ET": 75, "FR": 82, "ZA": 85, "IL": 72,
         "SO": 55, "RU": 45, "KP": 20}),
    "UG": _p("Uganda", "UG", 0.35, 32.58, "Kampala", "48M", "$53B", 69, "Presidential republic",
        [("President", "Yoweri Museveni"), ("PM", "Robinah Nabbanja"), ("Foreign Minister", "Jeje Odongo")],
        ["UN", "AU", "EAC", "COMESA", "IGAD", "Commonwealth", "Non-Aligned Movement", "OIC", "G77"],
        {"abortion": ("restricted", "Only to save life"),
         "lgbtq_rights": ("banned", "Harshest 2023 law with death penalty clause"),
         "death_penalty": ("active", "Retained"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("hosting", "Hosts 1.6M refugees—Africa's largest"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"CN": 82, "RU": 75, "TR": 75, "SA": 72, "KE": 80, "RW": 55, "TZ": 72, "SS": 80, "IN": 72, "US": 55,
         "GB": 60, "EU": 55, "KP": 25, "IL": 68}),
    "TZ": _p("Tanzania", "TZ", -6.17, 35.74, "Dodoma", "67M", "$79B", 55, "Presidential republic",
        [("President", "Samia Suluhu Hassan"), ("VP", "Philip Mpango"), ("Foreign Minister", "Mahmoud Thabit Kombo")],
        ["UN", "AU", "EAC", "SADC", "Commonwealth", "Non-Aligned Movement", "G77"],
        {"abortion": ("restricted", "Only to save life"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("active", "Retained; no executions since 1994"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("hosting", "Regional refugee host"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"KE": 82, "UG": 72, "CN": 82, "IN": 82, "SA": 78, "ZA": 85, "RW": 78, "MZ": 82, "BI": 72, "GB": 78,
         "RU": 55, "US": 65, "KP": 25, "IL": 55}),
    "RW": _p("Rwanda", "RW", -1.95, 30.06, "Kigali", "13M", "$14B", 88, "Presidential republic",
        [("President", "Paul Kagame"), ("PM", "Édouard Ngirente"), ("Foreign Minister", "Olivier Nduhungirehe")],
        ["UN", "AU", "EAC", "COMESA", "Commonwealth", "Francophonie", "Non-Aligned Movement", "G77"],
        {"abortion": ("restricted", "Rape/health only"),
         "lgbtq_rights": ("partial", "Not criminalized; no recognition"),
         "death_penalty": ("abolished", "Abolished 2007"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Mutuelles community health"),
         "climate": ("strong", "Plastic ban; Kigali Amendment host"),
         "immigration": ("hosting", "Refugee host"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"GB": 85, "US": 78, "CN": 82, "QA": 80, "TR": 78, "UG": 55, "KE": 82, "SG": 85, "IN": 78, "IL": 78,
         "CD": 5, "BI": 20, "RU": 45, "KP": 15}),
    "BI": _p("Burundi", "BI", -3.38, 29.36, "Gitega", "13M", "$4B", 135, "Presidential republic",
        [("President", "Évariste Ndayishimiye"), ("PM", "Gervais Ndirakobuca"), ("Foreign Minister", "Albert Shingiro")],
        ["UN", "AU", "EAC", "COMESA", "Francophonie", "Non-Aligned Movement", "G77"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "Criminalized 2009"),
         "death_penalty": ("abolished", "Abolished 2009"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("low", "Limited capacity"),
         "immigration": ("moderate", "Regional movement"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"RU": 72, "CN": 78, "TZ": 72, "KE": 72, "UG": 68, "FR": 68, "BE": 75, "CD": 62,
         "RW": 25, "US": 45, "IL": 45, "KP": 20}),
    "ZA": _p("South Africa", "ZA", -25.75, 28.19, "Pretoria/Cape Town/Bloemfontein", "62M", "$400B", 33, "Parliamentary republic",
        [("President", "Cyril Ramaphosa"), ("Deputy President", "Paul Mashatile"), ("Foreign Minister", "Ronald Lamola")],
        ["UN", "AU", "SADC", "Commonwealth", "G20", "BRICS", "Non-Aligned Movement", "G77", "IBSA"],
        {"abortion": ("legal", "Legal on request to 12 weeks (1996)"),
         "lgbtq_rights": ("legal", "Marriage equality 2006—first in Africa"),
         "death_penalty": ("abolished", "Abolished 1995"),
         "gun_control": ("moderate", "Licensing required"),
         "healthcare": ("mixed", "Public + private; NHI rolling out"),
         "climate": ("moderate", "Just Energy Transition Partnership"),
         "immigration": ("moderate", "Regional labor inflow"),
         "nuclear_stance": ("none", "Voluntarily denuclearized 1991—only state to do so")},
        {"BR": 82, "IN": 85, "CN": 85, "RU": 78, "ZW": 78, "NA": 88, "BW": 88, "MZ": 82, "NG": 78, "GB": 78, "DE": 80, "PS": 90,
         "IL": 8, "US": 65, "UA": 50, "KP": 30}),
})
# Africa batch 1 complete


# ═══════════════ AFRICA (part 2) ═══════════════
COUNTRY_PROFILES.update({
    "NG": _p("Nigeria", "NG", 9.08, 7.48, "Abuja", "223M", "$477B", 38, "Federal presidential republic",
        [("President", "Bola Tinubu"), ("VP", "Kashim Shettima"), ("Foreign Minister", "Yusuf Tuggar")],
        ["UN", "AU", "ECOWAS", "OPEC", "OIC", "Commonwealth", "G77", "Non-Aligned Movement"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "SSMPA 2014; death penalty in Sharia states"),
         "death_penalty": ("active", "Retained"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("moderate", "Regional host and source"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"US": 78, "GB": 85, "CN": 82, "IN": 80, "SA": 78, "BR": 75, "DE": 78, "FR": 70, "GH": 82, "ZA": 78, "SN": 80,
         "IL": 55, "RU": 55, "NE": 25, "ML": 25, "KP": 25}),
    "GH": _p("Ghana", "GH", 5.61, -0.19, "Accra", "34M", "$77B", 76, "Presidential republic",
        [("President", "John Mahama"), ("VP", "Jane Naana Opoku-Agyemang"), ("Foreign Minister", "Samuel Okudzeto Ablakwa")],
        ["UN", "AU", "ECOWAS", "Commonwealth", "Non-Aligned Movement", "G77"],
        {"abortion": ("restricted", "Broad grounds; hard access"),
         "lgbtq_rights": ("banned", "Harsh 2024 bill pending"),
         "death_penalty": ("abolished", "Abolished 2023"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "NHIS"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("open", "Year of Return"),
         "nuclear_stance": ("none", "NPT; Pelindaba; TPNW party")},
        {"US": 82, "GB": 88, "CN": 80, "NG": 80, "DE": 82, "CI": 85, "TG": 75, "BF": 72, "SN": 82, "IN": 78,
         "RU": 40, "IL": 55, "KP": 20}),
    "CI": _p("Côte d'Ivoire", "CI", 5.32, -4.03, "Yamoussoukro/Abidjan", "29M", "$79B", 75, "Presidential republic",
        [("President", "Alassane Ouattara"), ("PM", "Robert Beugré Mambé"), ("Foreign Minister", "Kacou Houadja Léon Adom")],
        ["UN", "AU", "ECOWAS", "WAEMU", "OIC", "Francophonie", "Non-Aligned Movement", "G77"],
        {"abortion": ("restricted", "Only to save life"),
         "lgbtq_rights": ("partial", "Not criminalized; no recognition"),
         "death_penalty": ("abolished", "Abolished 2000"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("moderate", "Major cocoa producer"),
         "immigration": ("open", "Regional labor"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"FR": 88, "US": 82, "GH": 80, "BF": 62, "ML": 55, "NE": 55, "SN": 85, "DE": 80, "CN": 78,
         "RU": 45, "IL": 55, "KP": 20}),
    "SN": _p("Senegal", "SN", 14.69, -17.45, "Dakar", "18M", "$32B", 90, "Presidential republic",
        [("President", "Bassirou Diomaye Faye"), ("PM", "Ousmane Sonko"), ("Foreign Minister", "Yassine Fall")],
        ["UN", "AU", "ECOWAS", "WAEMU", "OIC", "Francophonie", "Non-Aligned Movement", "G77"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("abolished", "Abolished 2004"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "CMU"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("moderate", "Regional migration"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"FR": 85, "US": 80, "MA": 85, "CN": 82, "SA": 78, "ML": 70, "GM": 82, "GN": 70, "MR": 72,
         "IL": 45, "RU": 50, "KP": 20}),
    "ML": _p("Mali", "ML", 12.65, -8.00, "Bamako", "22M", "$20B", 83, "Military junta",
        [("Transition President", "Assimi Goïta"), ("PM", "Abdoulaye Maïga"), ("Foreign Minister", "Abdoulaye Diop")],
        ["UN", "AU (suspended)", "AES (Sahel alliance)", "OIC", "G77"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "Criminalized 2024"),
         "death_penalty": ("active", "Retained; no executions since 1980"),
         "gun_control": ("weak", "Conflict zone"),
         "healthcare": ("weak", "Fragile"),
         "climate": ("low", "No capacity"),
         "immigration": ("emigration", "Conflict displacement"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"RU": 90, "BF": 92, "NE": 90, "CN": 72, "IR": 60, "VE": 65, "GN": 55,
         "FR": 5, "US": 25, "UA": 15, "IL": 20, "NG": 40, "CI": 40}),
    "BF": _p("Burkina Faso", "BF", 12.37, -1.52, "Ouagadougou", "23M", "$20B", 95, "Military junta",
        [("Interim President", "Ibrahim Traoré"), ("PM", "Apollinaire Kyélem"), ("Foreign Minister", "Karamoko Jean Marie Traoré")],
        ["UN", "AU (suspended)", "AES", "OIC", "G77"],
        {"abortion": ("restricted", "Rape/health only"),
         "lgbtq_rights": ("minimal", "Not criminalized; no recognition"),
         "death_penalty": ("abolished", "Abolished 2018"),
         "gun_control": ("weak", "Conflict zone"),
         "healthcare": ("weak", "Fragile"),
         "climate": ("low", "Sahel vulnerability"),
         "immigration": ("emigration", "IDPs from jihadist conflict"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"RU": 90, "ML": 92, "NE": 90, "CN": 72, "VE": 65, "IR": 60,
         "FR": 5, "US": 25, "UA": 15, "IL": 20, "CI": 40, "TG": 60}),
    "NE": _p("Niger", "NE", 13.52, 2.11, "Niamey", "26M", "$17B", 85, "Military junta",
        [("CNSP President", "Abdourahamane Tchiani"), ("PM", "Ali Mahamane Lamine Zeine"), ("Foreign Minister", "Bakary Yaou Sangaré")],
        ["UN", "AU (suspended)", "AES", "OIC", "G77"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("partial", "Not criminalized; no recognition"),
         "death_penalty": ("active", "Retained; no executions since 1976"),
         "gun_control": ("weak", "Conflict zone"),
         "healthcare": ("weak", "Fragile"),
         "climate": ("low", "Sahel vulnerability"),
         "immigration": ("moderate", "Key transit route"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"RU": 88, "ML": 90, "BF": 90, "CN": 72, "IR": 60, "TR": 72,
         "FR": 5, "US": 25, "UA": 15, "IL": 20, "NG": 15, "TD": 55}),
    "CM": _p("Cameroon", "CM", 3.85, 11.50, "Yaoundé", "28M", "$50B", 52, "Presidential republic",
        [("President", "Paul Biya"), ("PM", "Joseph Ngute"), ("Foreign Minister", "Lejeune Mbella Mbella")],
        ["UN", "AU", "ECCAS", "CEMAC", "Commonwealth", "Francophonie", "OIC", "G77"],
        {"abortion": ("restricted", "Rape/health only"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("active", "Retained"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("hosting", "Regional refugee host"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"FR": 80, "CN": 82, "US": 72, "RU": 72, "GB": 72, "NG": 50, "CF": 60, "TD": 62, "GA": 78, "CG": 78,
         "IL": 50, "KP": 25}),
    "CD": _p("DR Congo", "CD", -4.44, 15.27, "Kinshasa", "102M", "$69B", 84, "Semi-presidential republic",
        [("President", "Félix Tshisekedi"), ("PM", "Judith Suminwa Tuluka"), ("Foreign Minister", "Thérèse Kayikwamba Wagner")],
        ["UN", "AU", "SADC", "ECCAS", "EAC (joined 2022)", "COMESA", "Francophonie", "G77"],
        {"abortion": ("restricted", "Maputo Protocol partial"),
         "lgbtq_rights": ("partial", "Not criminalized; no recognition"),
         "death_penalty": ("active", "Moratorium lifted 2024"),
         "gun_control": ("weak", "Conflict in east"),
         "healthcare": ("weak", "Fragile"),
         "climate": ("moderate", "Carbon-sink basin"),
         "immigration": ("emigration", "Conflict displacement"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"BE": 72, "US": 70, "FR": 72, "CN": 82, "AO": 75, "CG": 85, "BI": 55, "ZA": 78, "TZ": 72, "ZM": 78,
         "RW": 5, "UG": 15, "KP": 25, "IL": 45, "RU": 55}),
    "CG": _p("Rep. Congo", "CG", -4.26, 15.28, "Brazzaville", "5.8M", "$14B", 100, "Presidential republic",
        [("President", "Denis Sassou Nguesso"), ("PM", "Anatole Collinet Makosso"), ("Foreign Minister", "Jean-Claude Gakosso")],
        ["UN", "AU", "ECCAS", "CEMAC", "OPEC", "Francophonie", "G77"],
        {"abortion": ("restricted", "Only to save life"),
         "lgbtq_rights": ("partial", "Not criminalized; no recognition"),
         "death_penalty": ("abolished", "Abolished 2015"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("moderate", "Congo Basin"),
         "immigration": ("moderate", "Regional"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"FR": 80, "CN": 88, "RU": 75, "AO": 78, "CD": 85, "GA": 82, "CM": 78, "CF": 70,
         "US": 55, "IL": 45, "KP": 25}),
    "AO": _p("Angola", "AO", -8.84, 13.23, "Luanda", "36M", "$106B", 61, "Presidential republic",
        [("President", "João Lourenço"), ("VP", "Esperança da Costa"), ("Foreign Minister", "Téte António")],
        ["UN", "AU", "SADC", "OPEC (left 2024)", "CPLP", "ECCAS", "G77"],
        {"abortion": ("restricted", "Rape/health only"),
         "lgbtq_rights": ("partial", "Decriminalized 2021"),
         "death_penalty": ("abolished", "Abolished 1992"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("moderate", "Regional"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"PT": 85, "CN": 88, "BR": 82, "RU": 75, "CD": 75, "NA": 82, "ZM": 82, "CG": 75, "ZA": 85, "CU": 80,
         "US": 72, "IL": 60, "KP": 30}),
    "ZM": _p("Zambia", "ZM", -15.42, 28.28, "Lusaka", "20M", "$30B", 115, "Presidential republic",
        [("President", "Hakainde Hichilema"), ("VP", "Mutale Nalumango"), ("Foreign Minister", "Mulambo Haimbe")],
        ["UN", "AU", "SADC", "COMESA", "Commonwealth", "Non-Aligned Movement", "G77"],
        {"abortion": ("restricted", "Broad grounds; access limited"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("abolished", "Abolished 2022"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("moderate", "Regional host"),
         "nuclear_stance": ("none", "NPT; Pelindaba; TPNW party")},
        {"US": 85, "GB": 85, "CN": 78, "ZA": 85, "BW": 85, "NA": 80, "AO": 82, "ZW": 72, "TZ": 82,
         "RU": 45, "IL": 60, "KP": 20}),
    "ZW": _p("Zimbabwe", "ZW", -17.82, 31.05, "Harare", "16M", "$30B", 72, "Presidential republic",
        [("President", "Emmerson Mnangagwa"), ("VP", "Constantino Chiwenga"), ("Foreign Minister", "Amon Murwira")],
        ["UN", "AU", "SADC", "COMESA", "Commonwealth (re-joining)", "Non-Aligned Movement", "G77"],
        {"abortion": ("restricted", "Rape/life only"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("abolished", "Abolished 2024"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private; strained"),
         "climate": ("low", "Limited capacity"),
         "immigration": ("emigration", "Economic crisis driven"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"CN": 92, "RU": 85, "ZA": 78, "MZ": 78, "NA": 75, "BY": 72, "IR": 68, "VE": 72, "ZM": 72,
         "US": 25, "GB": 25, "KP": 45, "IL": 35}),
    "MZ": _p("Mozambique", "MZ", -25.97, 32.57, "Maputo", "34M", "$20B", 106, "Presidential republic",
        [("President", "Daniel Chapo"), ("PM", "Maria Levy"), ("Foreign Minister", "Maria Lucas")],
        ["UN", "AU", "SADC", "Commonwealth", "CPLP", "OIC", "Non-Aligned Movement", "G77"],
        {"abortion": ("legal", "Legal on request to 12 weeks (2014)"),
         "lgbtq_rights": ("partial", "Decriminalized 2015"),
         "death_penalty": ("abolished", "Abolished 1990"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("low", "Cyclone-prone"),
         "immigration": ("moderate", "Regional"),
         "nuclear_stance": ("none", "NPT; Pelindaba; TPNW party")},
        {"PT": 85, "ZA": 85, "BR": 82, "CN": 85, "TZ": 82, "ZW": 78, "MW": 78, "SZ": 78,
         "RU": 55, "US": 72, "IL": 50, "KP": 25}),
    "NA": _p("Namibia", "NA", -22.57, 17.08, "Windhoek", "2.6M", "$13B", 120, "Presidential republic",
        [("President", "Netumbo Nandi-Ndaitwah"), ("PM", "Elijah Ngurare"), ("Foreign Minister", "Selma Ashipala-Musavyi")],
        ["UN", "AU", "SADC", "Commonwealth", "Non-Aligned Movement", "G77", "SACU"],
        {"abortion": ("restricted", "Apartheid-era law; rape/life only"),
         "lgbtq_rights": ("partial", "Decriminalized 2024 via court"),
         "death_penalty": ("abolished", "Abolished 1990"),
         "gun_control": ("moderate", "Licensing"),
         "healthcare": ("universal", "Public + private"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("moderate", "Regional"),
         "nuclear_stance": ("none", "NPT; Pelindaba; major uranium producer")},
        {"ZA": 92, "BW": 88, "AO": 82, "GB": 82, "DE": 82, "ZM": 78, "CN": 80, "US": 78, "BR": 78,
         "IL": 40, "RU": 55, "KP": 25}),
    "BW": _p("Botswana", "BW", -24.65, 25.91, "Gaborone", "2.7M", "$21B", 130, "Parliamentary republic",
        [("President", "Duma Boko"), ("VP", "Ndaba Gaolathe"), ("Foreign Minister", "Phenyo Butale")],
        ["UN", "AU", "SADC", "Commonwealth", "Non-Aligned Movement", "G77", "SACU"],
        {"abortion": ("restricted", "Rape/health only"),
         "lgbtq_rights": ("partial", "Decriminalized 2019"),
         "death_penalty": ("active", "Retained and active"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Public system"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("moderate", "Regional"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"ZA": 92, "NA": 88, "ZW": 75, "ZM": 82, "US": 85, "GB": 88, "DE": 85, "BR": 78, "IL": 72,
         "RU": 40, "KP": 25}),
    "LS": _p("Lesotho", "LS", -29.31, 27.48, "Maseru", "2.3M", "$2.5B", 160, "Constitutional monarchy",
        [("King", "Letsie III"), ("PM", "Sam Matekane"), ("Foreign Minister", "Lejone Mpotjoane")],
        ["UN", "AU", "SADC", "Commonwealth", "Non-Aligned Movement", "G77", "SACU"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("partial", "Not criminalized; no recognition"),
         "death_penalty": ("active", "Retained; no executions since 1995"),
         "gun_control": ("moderate", "Licensing"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("moderate", "Mountain kingdom"),
         "immigration": ("emigration", "Labor migration to SA"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"ZA": 98, "BW": 85, "NA": 78, "SZ": 82, "GB": 82, "US": 75, "EU": 75,
         "RU": 35, "KP": 20, "IL": 50}),
    "SZ": _p("Eswatini", "SZ", -26.52, 31.47, "Mbabane/Lobamba", "1.2M", "$4.5B", 180, "Absolute monarchy",
        [("King", "Mswati III"), ("PM", "Russell Dlamini")],
        ["UN", "AU", "SADC", "Commonwealth", "Non-Aligned Movement", "G77", "SACU"],
        {"abortion": ("restricted", "Rape/health only"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("active", "Retained; no executions since 1983"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("low", "Limited capacity"),
         "immigration": ("moderate", "Regional"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"ZA": 88, "MZ": 85, "TW": 92, "US": 78, "GB": 82, "BW": 82, "IL": 72,
         "CN": 20, "RU": 40, "KP": 20}),
    "TD": _p("Chad", "TD", 12.13, 15.05, "N'Djamena", "18M", "$13B", 83, "Presidential republic",
        [("President", "Mahamat Déby"), ("PM", "Allamaye Halina"), ("Foreign Minister", "Abderaman Koulamallah")],
        ["UN", "AU", "ECCAS", "CEMAC", "OIC", "Francophonie", "G5 Sahel", "G77"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "Criminalized 2017"),
         "death_penalty": ("abolished", "Abolished 2020"),
         "gun_control": ("weak", "Conflict-affected"),
         "healthcare": ("weak", "Fragile"),
         "climate": ("low", "Sahel vulnerability"),
         "immigration": ("hosting", "Regional refugee host"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"FR": 80, "US": 62, "CM": 72, "NG": 55, "CF": 45, "RU": 65, "CN": 75, "SD": 55,
         "LY": 40, "IL": 72, "NE": 55, "KP": 25}),
    "MR": _p("Mauritania", "MR", 18.07, -15.97, "Nouakchott", "4.9M", "$10B", 130, "Presidential republic",
        [("President", "Mohamed Ould Ghazouani"), ("PM", "Mokhtar Ould Djay"), ("Foreign Minister", "Mohamed Salem Ould Merzoug")],
        ["UN", "AU", "Arab League", "OIC", "Maghreb Union", "G77"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "Death penalty retained"),
         "death_penalty": ("active", "Retained; no executions since 1987"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("low", "Sahel vulnerability"),
         "immigration": ("transit", "EU route transit"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"SA": 82, "FR": 75, "MA": 80, "SN": 72, "ES": 75, "CN": 72, "QA": 75, "DZ": 72, "TR": 75,
         "IL": 15, "KP": 25, "RU": 45}),
    "LR": _p("Liberia", "LR", 6.31, -10.80, "Monrovia", "5.4M", "$4B", 170, "Presidential republic",
        [("President", "Joseph Boakai"), ("VP", "Jeremiah Koung"), ("Foreign Minister", "Sara Beysolow Nyanti")],
        ["UN", "AU", "ECOWAS", "Non-Aligned Movement", "G77"],
        {"abortion": ("restricted", "Rape/health only"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("active", "Retained but moratorium"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private; weak"),
         "climate": ("low", "Limited capacity"),
         "immigration": ("moderate", "Regional"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"US": 92, "GB": 80, "SL": 82, "GH": 82, "CI": 75, "GN": 72, "NG": 75, "EU": 78,
         "RU": 40, "CN": 60, "IL": 55, "KP": 15}),
    "SL": _p("Sierra Leone", "SL", 8.48, -13.23, "Freetown", "8.8M", "$4B", 165, "Presidential republic",
        [("President", "Julius Maada Bio"), ("VP", "Mohamed Juldeh Jalloh"), ("Foreign Minister", "Musa Timothy Kabba")],
        ["UN", "AU", "ECOWAS", "Commonwealth", "OIC", "Non-Aligned Movement", "G77"],
        {"abortion": ("restricted", "Rape/health only"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("abolished", "Abolished 2021"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Free for women/children"),
         "climate": ("low", "Limited capacity"),
         "immigration": ("moderate", "Regional"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"GB": 88, "US": 85, "CN": 72, "LR": 82, "GN": 72, "NG": 78, "GH": 78, "SN": 75,
         "RU": 40, "IL": 55, "KP": 15}),
    "GN": _p("Guinea", "GN", 9.65, -13.58, "Conakry", "14M", "$22B", 100, "Military junta",
        [("Transition President", "Mamadi Doumbouya"), ("PM", "Bah Oury"), ("Foreign Minister", "Morissanda Kouyaté")],
        ["UN", "AU (suspended)", "ECOWAS (suspended)", "OIC", "Francophonie", "G77"],
        {"abortion": ("restricted", "Rape/health only"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("abolished", "Abolished 2016"),
         "gun_control": ("weak", "Junta context"),
         "healthcare": ("weak", "Fragile"),
         "climate": ("low", "Limited capacity"),
         "immigration": ("moderate", "Regional"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"RU": 75, "CN": 82, "ML": 62, "BF": 62, "GW": 75, "SL": 72, "LR": 70, "AE": 72,
         "FR": 40, "US": 55, "IL": 50, "KP": 25}),
    "GM": _p("Gambia", "GM", 13.45, -16.58, "Banjul", "2.7M", "$2.4B", 175, "Presidential republic",
        [("President", "Adama Barrow"), ("VP", "Muhammad B.S. Jallow"), ("Foreign Minister", "Mamadou Tangara")],
        ["UN", "AU", "ECOWAS", "Commonwealth", "OIC", "Francophonie (assoc.)", "G77"],
        {"abortion": ("restricted", "Only to save life"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("abolished", "Moratorium since 2017"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("strong", "Only African state with Paris-compatible NDC"),
         "immigration": ("moderate", "Regional"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"SN": 90, "US": 80, "GB": 82, "CN": 72, "NG": 75, "GH": 78, "MR": 68,
         "IL": 40, "RU": 45, "KP": 20, "MM": 5}),
    "GW": _p("Guinea-Bissau", "GW", 11.86, -15.60, "Bissau", "2.1M", "$1.7B", 180, "Presidential republic",
        [("President", "Umaro Sissoco Embaló"), ("PM", "Rui Duarte de Barros")],
        ["UN", "AU", "ECOWAS", "WAEMU", "OIC", "CPLP", "Francophonie (obs.)", "G77"],
        {"abortion": ("legal", "Legal on request"),
         "lgbtq_rights": ("minimal", "Not criminalized; no recognition"),
         "death_penalty": ("abolished", "Abolished 1993"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("weak", "Fragile"),
         "climate": ("low", "Limited capacity"),
         "immigration": ("moderate", "Regional"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"PT": 85, "SN": 82, "GN": 75, "CN": 75, "BR": 78, "AO": 72, "GM": 72,
         "RU": 40, "US": 55, "IL": 45, "KP": 20}),
    "TG": _p("Togo", "TG", 6.17, 1.23, "Lomé", "8.7M", "$9B", 130, "Presidential republic",
        [("President of Council", "Faure Gnassingbé"), ("President", "Jean-Lucien Savi de Tové"), ("Foreign Minister", "Robert Dussey")],
        ["UN", "AU", "ECOWAS", "WAEMU", "OIC", "Francophonie", "G77"],
        {"abortion": ("restricted", "Rape/health only"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("abolished", "Abolished 2009"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("low", "Limited capacity"),
         "immigration": ("moderate", "Regional"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"FR": 78, "CN": 78, "US": 72, "BF": 82, "BJ": 82, "GH": 78, "CI": 75, "NE": 62,
         "RU": 55, "IL": 55, "KP": 25}),
    "BJ": _p("Benin", "BJ", 6.45, 2.36, "Porto-Novo/Cotonou", "13M", "$18B", 125, "Presidential republic",
        [("President", "Patrice Talon"), ("VP", "Mariam Chabi Talata"), ("Foreign Minister", "Olushegun Adjadi Bakari")],
        ["UN", "AU", "ECOWAS", "WAEMU", "OIC", "Francophonie", "G77"],
        {"abortion": ("legal", "Legalized 2021 broad grounds"),
         "lgbtq_rights": ("partial", "Not criminalized; no recognition"),
         "death_penalty": ("abolished", "Abolished 2016"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("moderate", "Paris signatory"),
         "immigration": ("moderate", "Regional"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"FR": 80, "CN": 82, "US": 75, "TG": 82, "NG": 72, "GH": 78, "NE": 55, "BF": 65,
         "RU": 45, "IL": 55, "KP": 20}),
    "GA": _p("Gabon", "GA", 0.42, 9.47, "Libreville", "2.4M", "$20B", 125, "Transitional military",
        [("Transition President", "Brice Oligui Nguema"), ("PM", "Raymond Ndong Sima"), ("Foreign Minister", "Régis Onanga Ndiaye")],
        ["UN", "AU (suspended)", "Commonwealth", "ECCAS", "CEMAC", "OPEC", "Francophonie", "G77"],
        {"abortion": ("restricted", "Rape/health only"),
         "lgbtq_rights": ("partial", "Decriminalized 2020"),
         "death_penalty": ("abolished", "Abolished 2010"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("strong", "88% forested; carbon-negative"),
         "immigration": ("moderate", "Regional"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"FR": 68, "CN": 82, "US": 72, "CM": 78, "CG": 80, "GQ": 72, "AO": 75,
         "RU": 55, "IL": 55, "KP": 25}),
    "CF": _p("C. African Rep.", "CF", 4.39, 18.56, "Bangui", "5.6M", "$2.8B", 160, "Presidential republic",
        [("President", "Faustin-Archange Touadéra"), ("PM", "Félix Moloua"), ("Foreign Minister", "Sylvie Baïpo-Temon")],
        ["UN", "AU", "ECCAS", "CEMAC", "OIC", "Francophonie", "G77"],
        {"abortion": ("restricted", "Rape/health only"),
         "lgbtq_rights": ("minimal", "Not criminalized; no recognition"),
         "death_penalty": ("abolished", "Abolished 2022"),
         "gun_control": ("weak", "Conflict zone"),
         "healthcare": ("weak", "Fragile"),
         "climate": ("low", "Limited capacity"),
         "immigration": ("emigration", "Conflict displacement"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"RU": 92, "CN": 75, "TD": 45, "CM": 70, "CG": 68, "SD": 55, "BY": 65,
         "FR": 20, "US": 40, "UA": 25, "IL": 45, "KP": 30}),
    "GQ": _p("Eq. Guinea", "GQ", 3.76, 8.78, "Malabo", "1.7M", "$12B", 140, "Presidential republic",
        [("President", "Teodoro Obiang"), ("VP", "Teodoro Nguema Obiang Mangue"), ("PM", "Manuela Roka Botey")],
        ["UN", "AU", "ECCAS", "CEMAC", "OPEC", "CPLP", "Francophonie", "G77"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "Criminalized 2024"),
         "death_penalty": ("abolished", "Abolished 2022"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("low", "Limited capacity"),
         "immigration": ("moderate", "Regional"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"CN": 85, "RU": 75, "ES": 72, "FR": 68, "GA": 72, "CM": 75, "BR": 78,
         "US": 55, "GB": 60, "IL": 45, "KP": 25}),
    "ST": _p("São Tomé", "ST", 0.34, 6.73, "São Tomé", "230K", "$0.6B", 185, "Semi-presidential republic",
        [("President", "Carlos Vila Nova"), ("PM", "Patrice Trovoada")],
        ["UN", "AU", "ECCAS", "CEEAC", "CPLP", "Francophonie (obs.)", "G77"],
        {"abortion": ("legal", "Legal up to 12 weeks"),
         "lgbtq_rights": ("partial", "Decriminalized 2012"),
         "death_penalty": ("abolished", "Abolished 1990"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Public system"),
         "climate": ("strong", "Small island state"),
         "immigration": ("moderate", "Regional"),
         "nuclear_stance": ("none", "NPT; Pelindaba; TPNW party")},
        {"PT": 90, "AO": 82, "CN": 78, "BR": 82, "CV": 85, "GW": 72, "GQ": 75,
         "US": 65, "RU": 45, "IL": 50, "KP": 20}),
    "KM": _p("Comoros", "KM", -11.70, 43.24, "Moroni", "870K", "$1.3B", 180, "Federal presidential republic",
        [("President", "Azali Assoumani"), ("VP", "Mmadi Ali"), ("Foreign Minister", "Dhoihir Dhoulkamal")],
        ["UN", "AU", "Arab League", "OIC", "Francophonie", "COMESA", "G77"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("abolished", "Moratorium since 1997"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("strong", "Climate-vulnerable island"),
         "immigration": ("moderate", "Regional"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"FR": 72, "SA": 85, "AE": 78, "TR": 78, "CN": 78, "MG": 72, "QA": 78, "EG": 75, "TZ": 72,
         "IL": 15, "KP": 25, "RU": 45}),
    "SC": _p("Seychelles", "SC", -4.62, 55.45, "Victoria", "100K", "$2B", 200, "Presidential republic",
        [("President", "Wavel Ramkalawan"), ("VP", "Ahmed Afif"), ("Foreign Minister", "Sylvestre Radegonde")],
        ["UN", "AU", "SADC", "COMESA", "Commonwealth", "Francophonie", "Non-Aligned Movement", "IORA", "G77"],
        {"abortion": ("legal", "Legal on request"),
         "lgbtq_rights": ("partial", "Decriminalized 2016"),
         "death_penalty": ("abolished", "Abolished 1993"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Free public"),
         "climate": ("strong", "Climate leader—small island"),
         "immigration": ("moderate", "Tourism economy"),
         "nuclear_stance": ("none", "NPT; Pelindaba; TPNW party")},
        {"FR": 82, "GB": 85, "IN": 88, "CN": 80, "US": 78, "AE": 82, "MU": 92, "ZA": 85, "MG": 80,
         "RU": 55, "IL": 55, "KP": 20}),
    "MU": _p("Mauritius", "MU", -20.16, 57.50, "Port Louis", "1.3M", "$14B", 155, "Parliamentary republic",
        [("President", "Dharam Gokhool"), ("PM", "Navin Ramgoolam"), ("Foreign Minister", "Dhananjay Ramful")],
        ["UN", "AU", "SADC", "COMESA", "Commonwealth", "Francophonie", "Non-Aligned Movement", "IORA", "G77"],
        {"abortion": ("restricted", "Rape/health only"),
         "lgbtq_rights": ("partial", "Decriminalized 2023"),
         "death_penalty": ("abolished", "Abolished 1995"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Free public"),
         "climate": ("strong", "Climate leader"),
         "immigration": ("moderate", "Managed"),
         "nuclear_stance": ("none", "NPT; Pelindaba; TPNW party")},
        {"IN": 92, "FR": 88, "GB": 88, "CN": 82, "ZA": 85, "US": 80, "MG": 82, "SC": 92, "KE": 78,
         "RU": 50, "IL": 55, "KP": 20}),
    "MG": _p("Madagascar", "MG", -18.93, 47.52, "Antananarivo", "30M", "$16B", 135, "Semi-presidential republic",
        [("President", "Andry Rajoelina"), ("PM", "Christian Ntsay"), ("Foreign Minister", "Rasata Rafaravavitafika")],
        ["UN", "AU", "SADC", "COMESA", "Francophonie", "Non-Aligned Movement", "IORA", "G77"],
        {"abortion": ("banned", "Total ban—one of strictest"),
         "lgbtq_rights": ("minimal", "Acts over 21 legal; no recognition"),
         "death_penalty": ("abolished", "Abolished 2015"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("low", "Climate-vulnerable"),
         "immigration": ("moderate", "Managed"),
         "nuclear_stance": ("none", "NPT; Pelindaba")},
        {"FR": 85, "CN": 80, "US": 75, "IN": 78, "ZA": 78, "MU": 82, "RU": 60, "TR": 72, "KM": 72,
         "IL": 55, "KP": 25}),
    "MW": _p("Malawi", "MW", -13.93, 33.77, "Lilongwe", "20M", "$13B", 150, "Presidential republic",
        [("President", "Lazarus Chakwera"), ("VP", "Michael Usi"), ("Foreign Minister", "Nancy Tembo")],
        ["UN", "AU", "SADC", "COMESA", "Commonwealth", "Non-Aligned Movement", "G77"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("abolished", "Moratorium; SC review 2024"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Free public"),
         "climate": ("low", "Limited capacity"),
         "immigration": ("moderate", "Regional"),
         "nuclear_stance": ("none", "NPT; Pelindaba; TPNW party")},
        {"US": 82, "GB": 85, "CN": 72, "ZA": 82, "ZM": 82, "TZ": 78, "MZ": 75, "BR": 72, "IL": 72,
         "RU": 35, "KP": 20}),
    "CV": _p("Cape Verde", "CV", 14.93, -23.51, "Praia", "600K", "$2.6B", 190, "Parliamentary republic",
        [("President", "José Maria Neves"), ("PM", "Ulisses Correia e Silva"), ("Foreign Minister", "Rui Figueiredo Soares")],
        ["UN", "AU", "ECOWAS", "CPLP", "Francophonie", "Non-Aligned Movement", "G77"],
        {"abortion": ("legal", "Legal on request"),
         "lgbtq_rights": ("partial", "Decriminalized 2004; no marriage"),
         "death_penalty": ("abolished", "Abolished 1981"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Public system"),
         "climate": ("strong", "Leader on renewables"),
         "immigration": ("moderate", "Diaspora economy"),
         "nuclear_stance": ("none", "NPT; Pelindaba; TPNW party")},
        {"PT": 92, "BR": 85, "US": 85, "GB": 78, "ES": 82, "SN": 82, "GW": 80,
         "RU": 35, "CN": 72, "IL": 55, "KP": 15}),
})
# Africa batch 2 complete


# ═══════════════ OCEANIA ═══════════════
COUNTRY_PROFILES.update({
    "NZ": _p("New Zealand", "NZ", -41.29, 174.78, "Wellington", "5.2M", "$254B", 76, "Parliamentary democracy",
        [("King", "Charles III"), ("Governor-General", "Cindy Kiro"), ("PM", "Christopher Luxon"), ("Foreign Minister", "Winston Peters")],
        ["UN", "Commonwealth", "Five Eyes", "APEC", "OECD", "CPTPP", "WTO", "ANZUS (suspended since 1986)", "Pacific Islands Forum", "RCEP"],
        {"abortion": ("legal", "Legal on request to 20 weeks (2020)"),
         "lgbtq_rights": ("legal", "Marriage equality 2013"),
         "death_penalty": ("abolished", "Abolished 1989"),
         "gun_control": ("strict", "Tightened after 2019 Christchurch"),
         "healthcare": ("universal", "Public system"),
         "climate": ("strong", "Net-zero by 2050 law"),
         "immigration": ("moderate", "Points-based"),
         "nuclear_stance": ("banned", "Nuclear-free since 1984; SPNFZ; TPNW party")},
        {"AU": 98, "US": 92, "GB": 94, "CA": 92, "JP": 88, "SG": 88, "KR": 85, "FJ": 88, "WS": 92, "TO": 88, "FR": 85, "DE": 85, "PG": 85,
         "RU": 10, "KP": 8, "IR": 20, "CN": 55}),
    "PG": _p("Papua New Guinea", "PG", -9.44, 147.18, "Port Moresby", "10M", "$35B", 100, "Parliamentary democracy",
        [("King", "Charles III"), ("Governor-General", "Bob Dadae"), ("PM", "James Marape"), ("Foreign Minister", "Justin Tkatchenko")],
        ["UN", "Commonwealth", "Pacific Islands Forum", "APEC", "ACP Group", "Non-Aligned Movement", "G77"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("abolished", "Abolished 2022"),
         "gun_control": ("moderate", "Licensing required"),
         "healthcare": ("mixed", "Public + private"),
         "climate": ("moderate", "Forest carbon focus"),
         "immigration": ("moderate", "Managed"),
         "nuclear_stance": ("none", "NPT; TPNW signatory")},
        {"AU": 95, "NZ": 88, "US": 82, "JP": 85, "SB": 78, "FJ": 82, "GB": 80, "IN": 75, "ID": 75,
         "CN": 62, "RU": 30, "KP": 15, "IR": 25}),
    "FJ": _p("Fiji", "FJ", -18.14, 178.44, "Suva", "930K", "$5.4B", 160, "Parliamentary republic",
        [("President", "Wiliame Katonivere"), ("PM", "Sitiveni Rabuka"), ("Foreign Minister", "Sitiveni Rabuka")],
        ["UN", "Commonwealth", "Pacific Islands Forum", "ACP Group", "Non-Aligned Movement", "G77"],
        {"abortion": ("restricted", "Broad grounds"),
         "lgbtq_rights": ("partial", "Decriminalized 2010"),
         "death_penalty": ("abolished", "Abolished 2015"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Free public"),
         "climate": ("strong", "Pacific climate leadership"),
         "immigration": ("moderate", "Managed"),
         "nuclear_stance": ("none", "NPT; SPNFZ; TPNW party")},
        {"AU": 92, "NZ": 92, "US": 82, "JP": 85, "IN": 80, "GB": 85, "PG": 82, "WS": 88, "SB": 82, "TO": 85,
         "CN": 62, "RU": 35, "KP": 15, "IR": 25}),
    "SB": _p("Solomon Islands", "SB", -9.64, 160.16, "Honiara", "740K", "$1.7B", 190, "Parliamentary democracy",
        [("King", "Charles III"), ("Governor-General", "David Vunagi"), ("PM", "Jeremiah Manele")],
        ["UN", "Commonwealth", "Pacific Islands Forum", "ACP Group", "Non-Aligned Movement", "G77"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("abolished", "Abolished 1978"),
         "gun_control": ("strict", "Ban after ethnic tensions"),
         "healthcare": ("universal", "Free public"),
         "climate": ("strong", "Vulnerable island state"),
         "immigration": ("moderate", "Managed"),
         "nuclear_stance": ("none", "NPT; SPNFZ; TPNW party")},
        {"CN": 85, "AU": 62, "PG": 78, "NZ": 78, "FJ": 82, "JP": 78, "ID": 72,
         "US": 58, "TW": 15, "GB": 72, "KP": 20}),
    "VU": _p("Vanuatu", "VU", -17.73, 168.31, "Port Vila", "330K", "$1B", 195, "Parliamentary republic",
        [("President", "Nikenike Vurobaravu"), ("PM", "Charlot Salwai"), ("Foreign Minister", "Matai Seremaiah")],
        ["UN", "Commonwealth", "Pacific Islands Forum", "Francophonie", "ACP Group", "Non-Aligned Movement", "G77"],
        {"abortion": ("restricted", "Only to save life/health"),
         "lgbtq_rights": ("partial", "Not criminalized; no recognition"),
         "death_penalty": ("abolished", "Abolished 1980"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Public system"),
         "climate": ("strong", "Led ICJ climate opinion 2024"),
         "immigration": ("moderate", "Citizenship-by-investment"),
         "nuclear_stance": ("banned", "Nuclear-free; SPNFZ; TPNW party")},
        {"AU": 85, "NZ": 88, "FR": 82, "FJ": 85, "NC": 80, "JP": 80, "CN": 72, "PG": 75, "SB": 82,
         "US": 65, "RU": 35, "KP": 15, "IR": 20}),
    "WS": _p("Samoa", "WS", -13.76, -172.10, "Apia", "220K", "$0.9B", 200, "Parliamentary republic",
        [("O le Ao", "Tuimalealiifano Vaaletoa Sualauvi II"), ("PM", "Fiamē Naomi Mataʻafa")],
        ["UN", "Commonwealth", "Pacific Islands Forum", "ACP Group", "Non-Aligned Movement", "G77"],
        {"abortion": ("restricted", "Only to save health"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("abolished", "Abolished 2004"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Public system"),
         "climate": ("strong", "Highly vulnerable"),
         "immigration": ("moderate", "Diaspora in NZ"),
         "nuclear_stance": ("none", "NPT; SPNFZ; TPNW party")},
        {"NZ": 95, "AU": 90, "US": 82, "FJ": 88, "TO": 88, "JP": 85, "CN": 72, "TW": 60,
         "RU": 30, "KP": 15, "IR": 20}),
    "TO": _p("Tonga", "TO", -21.14, -175.19, "Nuku'alofa", "106K", "$0.5B", 200, "Constitutional monarchy",
        [("King", "Tupou VI"), ("PM", "ʻAisake Valu Eke"), ("Foreign Minister", "Fekitamoeloa Katoa ʻUtoikamanu")],
        ["UN", "Commonwealth", "Pacific Islands Forum", "ACP Group", "Non-Aligned Movement", "G77"],
        {"abortion": ("banned", "Only to save health"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("active", "Retained; no executions since 1982"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Public system"),
         "climate": ("strong", "Vulnerable island"),
         "immigration": ("moderate", "Diaspora"),
         "nuclear_stance": ("none", "NPT; SPNFZ")},
        {"NZ": 92, "AU": 88, "US": 82, "JP": 85, "CN": 72, "FJ": 85, "WS": 88, "GB": 82,
         "TW": 20, "RU": 30, "KP": 15, "IR": 20}),
    "KI": _p("Kiribati", "KI", 1.45, 172.97, "South Tarawa", "130K", "$0.2B", 205, "Presidential republic",
        [("President", "Taneti Maamau"), ("VP", "Teuea Toatu")],
        ["UN", "Commonwealth", "Pacific Islands Forum", "ACP Group", "Non-Aligned Movement", "G77", "AOSIS"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("abolished", "Abolished 1979"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Public system"),
         "climate": ("strong", "Existential threat"),
         "immigration": ("moderate", "Climate migration planning"),
         "nuclear_stance": ("none", "NPT; SPNFZ")},
        {"CN": 82, "AU": 78, "NZ": 80, "US": 75, "JP": 82, "FJ": 80, "NR": 80,
         "TW": 15, "RU": 30, "KP": 15, "IR": 20}),
    "TV": _p("Tuvalu", "TV", -8.52, 179.20, "Funafuti", "11K", "$65M", 210, "Parliamentary democracy",
        [("King", "Charles III"), ("Governor-General", "Tofiga Vaevalu Falani"), ("PM", "Feleti Teo")],
        ["UN", "Commonwealth", "Pacific Islands Forum", "ACP Group", "G77", "AOSIS"],
        {"abortion": ("restricted", "Only to save life"),
         "lgbtq_rights": ("banned", "Criminalized"),
         "death_penalty": ("abolished", "Abolished 1976"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Public system"),
         "climate": ("strong", "Sinking; Falepili Union with AU"),
         "immigration": ("emigration", "Climate migration to AU via Falepili"),
         "nuclear_stance": ("none", "NPT; SPNFZ; TPNW party")},
        {"AU": 95, "NZ": 92, "TW": 95, "US": 82, "FJ": 85, "JP": 85, "GB": 88, "KI": 85,
         "CN": 15, "RU": 30, "KP": 15, "IR": 20}),
    "NR": _p("Nauru", "NR", -0.55, 166.92, "Yaren (de facto)", "13K", "$150M", 210, "Parliamentary republic",
        [("President", "David Adeang")],
        ["UN", "Commonwealth", "Pacific Islands Forum", "ACP Group", "G77", "AOSIS"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("partial", "Decriminalized 2016"),
         "death_penalty": ("abolished", "Abolished 2016"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Public system"),
         "climate": ("strong", "Existential threat"),
         "immigration": ("hosting", "AU offshore processing"),
         "nuclear_stance": ("none", "NPT; SPNFZ; TPNW party")},
        {"AU": 90, "CN": 82, "US": 75, "NZ": 85, "JP": 82, "KI": 82, "FJ": 82,
         "TW": 10, "RU": 30, "KP": 15, "IR": 20}),
    "MH": _p("Marshall Islands", "MH", 7.09, 171.38, "Majuro", "42K", "$280M", 210, "Presidential republic",
        [("President", "Hilda Heine"), ("Foreign Minister", "Kalani Kaneko")],
        ["UN", "Pacific Islands Forum", "ACP Group", "G77", "AOSIS", "Compact of Free Association (US)"],
        {"abortion": ("banned", "Only to save life"),
         "lgbtq_rights": ("partial", "Decriminalized 2005"),
         "death_penalty": ("abolished", "Abolished 1986"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Public system"),
         "climate": ("strong", "Nuclear test legacy"),
         "immigration": ("moderate", "Free movement with US"),
         "nuclear_stance": ("banned", "Former US test site; strong disarmament advocate")},
        {"US": 95, "AU": 85, "JP": 88, "TW": 90, "NZ": 82, "FM": 95, "PW": 95, "KI": 80, "FJ": 80,
         "CN": 15, "RU": 25, "KP": 5, "IR": 15}),
    "FM": _p("Micronesia", "FM", 6.92, 158.16, "Palikir", "105K", "$430M", 210, "Federal presidential republic",
        [("President", "Wesley Simina"), ("VP", "Aren Palik")],
        ["UN", "Pacific Islands Forum", "ACP Group", "G77", "AOSIS", "Compact of Free Association (US)"],
        {"abortion": ("restricted", "Only to save life"),
         "lgbtq_rights": ("partial", "Decriminalized 2004"),
         "death_penalty": ("abolished", "Abolished 1986"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Public system"),
         "climate": ("strong", "Vulnerable island state"),
         "immigration": ("moderate", "Free movement with US"),
         "nuclear_stance": ("none", "NPT; SPNFZ; TPNW party")},
        {"US": 95, "AU": 85, "JP": 90, "NZ": 82, "MH": 92, "PW": 92, "FJ": 80, "KI": 80,
         "CN": 15, "RU": 25, "KP": 10, "IR": 15}),
    "PW": _p("Palau", "PW", 7.50, 134.62, "Ngerulmud", "18K", "$230M", 210, "Presidential republic",
        [("President", "Surangel Whipps Jr."), ("VP", "Uduch Sengebau Senior")],
        ["UN", "Pacific Islands Forum", "ACP Group", "G77", "AOSIS", "Compact of Free Association (US)"],
        {"abortion": ("restricted", "Only to save life"),
         "lgbtq_rights": ("partial", "Decriminalized 2014"),
         "death_penalty": ("abolished", "Abolished 1991"),
         "gun_control": ("strict", "Tight"),
         "healthcare": ("universal", "Public system"),
         "climate": ("strong", "Reef nation, climate leader"),
         "immigration": ("moderate", "Tourism economy"),
         "nuclear_stance": ("banned", "Constitution bans nuclear weapons; TPNW party")},
        {"US": 95, "AU": 85, "JP": 92, "TW": 95, "NZ": 82, "MH": 92, "FM": 92, "PH": 80, "KR": 82,
         "CN": 10, "RU": 25, "KP": 5, "IR": 15}),
})
# Oceania batch complete


NUCLEAR_ARSENALS = [
    {"country": "Russia", "warheads": 5580, "status": "Modernizing", "flag": "RU"},
    {"country": "United States", "warheads": 5044, "status": "Modernizing", "flag": "US"},
    {"country": "China", "warheads": 500, "status": "Rapidly expanding", "flag": "CN"},
    {"country": "France", "warheads": 290, "status": "Stable", "flag": "FR"},
    {"country": "United Kingdom", "warheads": 225, "status": "Stable", "flag": "GB"},
    {"country": "Pakistan", "warheads": 170, "status": "Growing", "flag": "PK"},
    {"country": "India", "warheads": 172, "status": "Growing", "flag": "IN"},
    {"country": "Israel", "warheads": 90, "status": "Undeclared", "flag": "IL"},
    {"country": "North Korea", "warheads": 50, "status": "Expanding", "flag": "KP"},
]


# ═══════════════ URANIUM ENRICHMENT & NUCLEAR FACILITIES ═══════════════
# Known enrichment, reprocessing, and key nuclear facilities (IAEA/OSINT)
NUCLEAR_FACILITIES = [
    # ── United States ──
    {"name": "Portsmouth GDP (decommissioned)", "country": "US", "lat": 38.73, "lng": -82.99, "type": "enrichment", "status": "decommissioned", "detail": "Former gaseous diffusion; D&D ongoing"},
    {"name": "Paducah GDP (decommissioned)", "country": "US", "lat": 37.12, "lng": -88.81, "type": "enrichment", "status": "decommissioned", "detail": "Former gaseous diffusion; cleanup ongoing"},
    {"name": "URENCO Eunice (NM)", "country": "US", "lat": 32.38, "lng": -103.19, "type": "enrichment", "status": "active", "detail": "Gas centrifuge; LEU production"},
    {"name": "Centrus Piketon (OH)", "country": "US", "lat": 39.01, "lng": -83.00, "type": "enrichment", "status": "active", "detail": "HALEU demonstration cascade"},
    {"name": "Y-12 National Security Complex", "country": "US", "lat": 35.98, "lng": -84.24, "type": "weapons", "status": "active", "detail": "HEU components; weapons secondaries"},
    {"name": "Pantex Plant", "country": "US", "lat": 35.32, "lng": -101.95, "type": "weapons", "status": "active", "detail": "Primary US nuclear weapons assembly/disassembly"},
    {"name": "Savannah River Site", "country": "US", "lat": 33.35, "lng": -81.74, "type": "reprocessing", "status": "active", "detail": "Tritium production; MOX (cancelled); waste processing"},
    {"name": "Hanford Site", "country": "US", "lat": 46.55, "lng": -119.49, "type": "reprocessing", "status": "cleanup", "detail": "Former Pu production; largest cleanup site"},
    {"name": "Idaho National Lab", "country": "US", "lat": 43.52, "lng": -112.94, "type": "research", "status": "active", "detail": "Nuclear R&D; advanced reactor testing"},
    {"name": "Los Alamos National Lab", "country": "US", "lat": 35.84, "lng": -106.29, "type": "weapons", "status": "active", "detail": "Pu pit production; weapons design"},
    {"name": "Lawrence Livermore NL", "country": "US", "lat": 37.69, "lng": -121.70, "type": "weapons", "status": "active", "detail": "Weapons design; NIF (fusion)"},
    {"name": "Sandia National Labs", "country": "US", "lat": 34.96, "lng": -106.51, "type": "weapons", "status": "active", "detail": "Non-nuclear weapons components; engineering"},
    # ── Russia ──
    {"name": "Novouralsk (Sverdlovsk-44)", "country": "RU", "lat": 57.24, "lng": 60.08, "type": "enrichment", "status": "active", "detail": "UEIP; largest centrifuge plant"},
    {"name": "Seversk (Tomsk-7)", "country": "RU", "lat": 56.60, "lng": 84.89, "type": "enrichment", "status": "active", "detail": "SCC; enrichment + reprocessing"},
    {"name": "Zelenogorsk (Krasnoyarsk-45)", "country": "RU", "lat": 56.12, "lng": 94.58, "type": "enrichment", "status": "active", "detail": "ECP; centrifuge enrichment"},
    {"name": "Angarsk ECC", "country": "RU", "lat": 52.48, "lng": 103.89, "type": "enrichment", "status": "active", "detail": "AECC; IAEA LEU Bank facility"},
    {"name": "Mayak (Chelyabinsk-65)", "country": "RU", "lat": 55.72, "lng": 60.82, "type": "reprocessing", "status": "active", "detail": "Pu reprocessing; HLW vitrification"},
    {"name": "Sarov (Arzamas-16)", "country": "RU", "lat": 54.93, "lng": 43.32, "type": "weapons", "status": "active", "detail": "RFNC-VNIIEF; primary weapons design lab"},
    {"name": "Snezhinsk (Chelyabinsk-70)", "country": "RU", "lat": 56.08, "lng": 60.73, "type": "weapons", "status": "active", "detail": "RFNC-VNIITF; secondary weapons lab"},
    {"name": "Lesnoy (Sverdlovsk-45)", "country": "RU", "lat": 58.63, "lng": 59.78, "type": "weapons", "status": "active", "detail": "Weapons assembly"},
    {"name": "Trekhgorny (Zlatoust-36)", "country": "RU", "lat": 54.81, "lng": 58.45, "type": "weapons", "status": "active", "detail": "Weapons assembly"},
    {"name": "Novaya Zemlya Test Site", "country": "RU", "lat": 73.37, "lng": 54.70, "type": "test_site", "status": "standby", "detail": "Nuclear weapons test site; 224 tests conducted"},
    # ── China ──
    {"name": "Lanzhou (Plant 504)", "country": "CN", "lat": 36.06, "lng": 103.74, "type": "enrichment", "status": "active", "detail": "Centrifuge enrichment; expanding"},
    {"name": "Hanzhong (Plant 405)", "country": "CN", "lat": 33.23, "lng": 106.67, "type": "enrichment", "status": "active", "detail": "Centrifuge enrichment; HEU-capable"},
    {"name": "Emeishan (Plant 814)", "country": "CN", "lat": 29.58, "lng": 103.44, "type": "enrichment", "status": "active", "detail": "Newer centrifuge facility"},
    {"name": "Haiyan (Plant 221, decom.)", "country": "CN", "lat": 36.97, "lng": 100.35, "type": "weapons", "status": "decommissioned", "detail": "First Chinese nuclear weapons plant"},
    {"name": "Mianyang (CAEP)", "country": "CN", "lat": 31.46, "lng": 104.74, "type": "weapons", "status": "active", "detail": "China Academy of Engineering Physics; weapons design"},
    {"name": "Guangyuan (Plant 821)", "country": "CN", "lat": 32.43, "lng": 105.84, "type": "reprocessing", "status": "active", "detail": "Military Pu production"},
    {"name": "Jiuquan (Plant 404)", "country": "CN", "lat": 40.17, "lng": 97.05, "type": "reprocessing", "status": "active", "detail": "Civilian pilot reprocessing"},
    {"name": "Lop Nur Test Site", "country": "CN", "lat": 41.57, "lng": 88.31, "type": "test_site", "status": "standby", "detail": "45 nuclear tests; moratorium since 1996"},
    # ── France ──
    {"name": "Georges Besse II (Tricastin)", "country": "FR", "lat": 44.33, "lng": 4.72, "type": "enrichment", "status": "active", "detail": "AREVA/Orano; centrifuge LEU"},
    {"name": "La Hague", "country": "FR", "lat": 49.68, "lng": -1.88, "type": "reprocessing", "status": "active", "detail": "Largest commercial reprocessing; UP2/UP3"},
    {"name": "Marcoule", "country": "FR", "lat": 44.14, "lng": 4.70, "type": "reprocessing", "status": "active", "detail": "CEA; MOX fuel; defense legacy"},
    {"name": "Valduc", "country": "FR", "lat": 47.49, "lng": 4.91, "type": "weapons", "status": "active", "detail": "Warhead assembly/disassembly; Pu/tritium"},
    {"name": "CEA DAM Île-de-France", "country": "FR", "lat": 48.73, "lng": 2.18, "type": "weapons", "status": "active", "detail": "Nuclear weapons design (Bruyères-le-Châtel)"},
    # ── UK ──
    {"name": "Capenhurst (URENCO)", "country": "GB", "lat": 53.26, "lng": -2.95, "type": "enrichment", "status": "active", "detail": "URENCO UK; centrifuge enrichment"},
    {"name": "Sellafield", "country": "GB", "lat": 54.42, "lng": -3.50, "type": "reprocessing", "status": "decommissioning", "detail": "THORP closed 2018; legacy waste management"},
    {"name": "AWE Aldermaston", "country": "GB", "lat": 51.37, "lng": -1.15, "type": "weapons", "status": "active", "detail": "Warhead design; Pu fabrication"},
    {"name": "AWE Burghfield", "country": "GB", "lat": 51.41, "lng": -1.02, "type": "weapons", "status": "active", "detail": "Warhead assembly"},
    # ── Pakistan ──
    {"name": "Kahuta (KRL)", "country": "PK", "lat": 33.60, "lng": 73.39, "type": "enrichment", "status": "active", "detail": "A.Q. Khan Research Labs; HEU for weapons"},
    {"name": "Gadwal", "country": "PK", "lat": 31.97, "lng": 72.35, "type": "enrichment", "status": "active", "detail": "Second enrichment site"},
    {"name": "Khushab Complex", "country": "PK", "lat": 32.02, "lng": 72.22, "type": "reprocessing", "status": "active", "detail": "4 Pu-production reactors + reprocessing"},
    {"name": "Chashma", "country": "PK", "lat": 32.39, "lng": 71.38, "type": "power", "status": "active", "detail": "4 CNP reactors (Chinese-supplied)"},
    # ── India ──
    {"name": "RMP Ratnahalli", "country": "IN", "lat": 12.26, "lng": 76.71, "type": "enrichment", "status": "active", "detail": "Gas centrifuge; expanding"},
    {"name": "BARC Trombay", "country": "IN", "lat": 19.01, "lng": 72.92, "type": "reprocessing", "status": "active", "detail": "Pu separation; Dhruva reactor"},
    {"name": "IGCAR Kalpakkam", "country": "IN", "lat": 12.56, "lng": 80.17, "type": "reprocessing", "status": "active", "detail": "Fast breeder; reprocessing"},
    {"name": "Pokhran Test Site", "country": "IN", "lat": 26.73, "lng": 71.73, "type": "test_site", "status": "standby", "detail": "Smiling Buddha (1974) + Shakti (1998)"},
    # ── Israel ──
    {"name": "Dimona (Negev NRC)", "country": "IL", "lat": 31.00, "lng": 35.15, "type": "reprocessing", "status": "active", "detail": "Pu production; undeclared weapons program"},
    # ── North Korea ──
    {"name": "Yongbyon Nuclear Complex", "country": "KP", "lat": 39.80, "lng": 125.75, "type": "enrichment", "status": "active", "detail": "Centrifuge enrichment + 5MWe reactor + reprocessing"},
    {"name": "Kangson (suspected)", "country": "KP", "lat": 39.03, "lng": 125.76, "type": "enrichment", "status": "suspected", "detail": "Suspected covert enrichment site"},
    {"name": "Punggye-ri Test Site", "country": "KP", "lat": 41.28, "lng": 129.09, "type": "test_site", "status": "demolished", "detail": "6 nuclear tests (2006-2017); tunnels demolished 2018"},
    # ── Iran ──
    {"name": "Natanz FEP", "country": "IR", "lat": 33.72, "lng": 51.73, "type": "enrichment", "status": "active", "detail": "Main centrifuge plant; underground halls; enriching to 60%"},
    {"name": "Fordow FFEP", "country": "IR", "lat": 34.88, "lng": 51.58, "type": "enrichment", "status": "active", "detail": "Underground mountain facility; enriching to 60%"},
    {"name": "Isfahan UCF", "country": "IR", "lat": 32.69, "lng": 51.69, "type": "conversion", "status": "active", "detail": "UF6 conversion; fuel fabrication"},
    {"name": "Arak IR-40 (redesigned)", "country": "IR", "lat": 34.05, "lng": 49.24, "type": "research", "status": "active", "detail": "Heavy water reactor (redesigned under JCPOA)"},
    {"name": "Parchin Military Complex", "country": "IR", "lat": 35.52, "lng": 51.77, "type": "weapons", "status": "suspected", "detail": "IAEA flagged; suspected EBW/implosion testing"},
    # ── Other ──
    {"name": "URENCO Gronau", "country": "DE", "lat": 52.20, "lng": 7.04, "type": "enrichment", "status": "active", "detail": "URENCO Germany; centrifuge LEU"},
    {"name": "URENCO Almelo", "country": "NL", "lat": 52.36, "lng": 6.62, "type": "enrichment", "status": "active", "detail": "URENCO Netherlands; centrifuge LEU"},
    {"name": "Rokkasho", "country": "JP", "lat": 40.96, "lng": 141.33, "type": "reprocessing", "status": "construction", "detail": "JNFL; delayed commercial reprocessing"},
    {"name": "Yongwang/Wolsong", "country": "KR", "lat": 35.72, "lng": 129.48, "type": "power", "status": "active", "detail": "PHWR cluster; Pu concern (spent fuel)"},
    {"name": "Pelindaba", "country": "ZA", "lat": -25.80, "lng": 27.93, "type": "research", "status": "active", "detail": "NECSA; former weapons program site (dismantled)"},
]


# ═══════════════ MILITARY BASES & TRAINING CENTERS ═══════════════
# Major military installations globally (OSINT / open sources)
MILITARY_BASES = [
    # ── US Major Overseas ──
    {"name": "Ramstein Air Base", "country": "US", "host": "DE", "lat": 49.44, "lng": 7.60, "type": "air_base", "branch": "USAF", "detail": "HQ USAFE-AFAFRICA; C2 hub for Europe/Africa"},
    {"name": "Camp Humphreys", "country": "US", "host": "KR", "lat": 36.96, "lng": 127.03, "type": "army_base", "branch": "US Army", "detail": "Largest overseas US base; HQ USFK"},
    {"name": "Kadena Air Base", "country": "US", "host": "JP", "lat": 26.35, "lng": 127.77, "type": "air_base", "branch": "USAF", "detail": "Largest US air base in Pacific"},
    {"name": "Yokosuka Naval Base", "country": "US", "host": "JP", "lat": 35.28, "lng": 139.66, "type": "naval_base", "branch": "USN", "detail": "HQ 7th Fleet; CVN-76 homeport"},
    {"name": "Diego Garcia", "country": "US", "host": "IO", "lat": -7.32, "lng": 72.42, "type": "air_base", "branch": "USN/USAF", "detail": "Indian Ocean strategic hub; B-2 capable"},
    {"name": "Al Udeid Air Base", "country": "US", "host": "QA", "lat": 25.12, "lng": 51.32, "type": "air_base", "branch": "USAF", "detail": "CAOC; largest US base in Middle East"},
    {"name": "Camp Lemonnier", "country": "US", "host": "DJ", "lat": 11.55, "lng": 43.15, "type": "military_base", "branch": "CJTF-HOA", "detail": "Only permanent US base in Africa"},
    {"name": "Naval Station Rota", "country": "US", "host": "ES", "lat": 36.62, "lng": -6.35, "type": "naval_base", "branch": "USN", "detail": "BMD destroyers; 6th Fleet support"},
    {"name": "Incirlik Air Base", "country": "US", "host": "TR", "lat": 37.00, "lng": 35.43, "type": "air_base", "branch": "USAF", "detail": "B61 nuclear weapons storage"},
    {"name": "Thule Space Base", "country": "US", "host": "GL", "lat": 76.53, "lng": -68.70, "type": "space_base", "branch": "USSF", "detail": "Ballistic missile early warning; satellite tracking"},
    {"name": "Guantánamo Bay", "country": "US", "host": "CU", "lat": 19.91, "lng": -75.10, "type": "naval_base", "branch": "USN", "detail": "Oldest overseas US base; detention facility"},
    {"name": "NSA Bahrain", "country": "US", "host": "BH", "lat": 26.21, "lng": 50.60, "type": "naval_base", "branch": "USN", "detail": "HQ 5th Fleet; NAVCENT"},
    # ── US Domestic Major ──
    {"name": "Fort Liberty (Bragg)", "country": "US", "host": "US", "lat": 35.14, "lng": -79.00, "type": "army_base", "branch": "US Army", "detail": "82nd Airborne; USASOC; JSOC"},
    {"name": "Fort Cavazos (Hood)", "country": "US", "host": "US", "lat": 31.14, "lng": -97.78, "type": "army_base", "branch": "US Army", "detail": "III Corps; 1st Cavalry Division"},
    {"name": "Norfolk Naval Station", "country": "US", "host": "US", "lat": 36.95, "lng": -76.33, "type": "naval_base", "branch": "USN", "detail": "World's largest naval base"},
    {"name": "San Diego Naval Base", "country": "US", "host": "US", "lat": 32.68, "lng": -117.13, "type": "naval_base", "branch": "USN", "detail": "Pacific Fleet major homeport"},
    {"name": "Joint Base Pearl Harbor-Hickam", "country": "US", "host": "US", "lat": 21.35, "lng": -157.95, "type": "naval_base", "branch": "USN/USAF", "detail": "HQ USINDOPACOM"},
    {"name": "Nellis AFB / Area 51", "country": "US", "host": "US", "lat": 36.24, "lng": -115.03, "type": "air_base", "branch": "USAF", "detail": "USAF Warfare Center; Red Flag; classified testing"},
    {"name": "Peterson SFB", "country": "US", "host": "US", "lat": 38.80, "lng": -104.70, "type": "space_base", "branch": "USSF", "detail": "HQ USSPACECOM & USNORTHCOM"},
    # ── Russia Major ──
    {"name": "Kaliningrad (Baltic Fleet)", "country": "RU", "host": "RU", "lat": 54.71, "lng": 20.51, "type": "naval_base", "branch": "VMF", "detail": "Baltic Fleet HQ; Iskander deployment"},
    {"name": "Severomorsk (Northern Fleet)", "country": "RU", "host": "RU", "lat": 69.07, "lng": 33.42, "type": "naval_base", "branch": "VMF", "detail": "Northern Fleet HQ; SSBN base"},
    {"name": "Vladivostok (Pacific Fleet)", "country": "RU", "host": "RU", "lat": 43.12, "lng": 131.90, "type": "naval_base", "branch": "VMF", "detail": "Pacific Fleet HQ"},
    {"name": "Sevastopol (Black Sea Fleet)", "country": "RU", "host": "UA/RU", "lat": 44.62, "lng": 33.52, "type": "naval_base", "branch": "VMF", "detail": "Black Sea Fleet HQ (disputed)"},
    {"name": "Tartus Naval Facility", "country": "RU", "host": "SY", "lat": 34.89, "lng": 35.89, "type": "naval_base", "branch": "VMF", "detail": "Only Russian Mediterranean base (status uncertain post-Assad)"},
    {"name": "Hmeimim Air Base", "country": "RU", "host": "SY", "lat": 35.41, "lng": 35.95, "type": "air_base", "branch": "VKS", "detail": "Russian air operations hub (status uncertain)"},
    {"name": "Engels-2 Air Base", "country": "RU", "host": "RU", "lat": 51.48, "lng": 46.20, "type": "air_base", "branch": "VKS", "detail": "Tu-160 / Tu-95MS strategic bombers"},
    {"name": "Plesetsk Cosmodrome", "country": "RU", "host": "RU", "lat": 62.93, "lng": 40.58, "type": "space_base", "branch": "VKS", "detail": "ICBM testing; military space launches"},
    # ── China Major ──
    {"name": "Yulin Naval Base (Hainan)", "country": "CN", "host": "CN", "lat": 18.22, "lng": 109.55, "type": "naval_base", "branch": "PLAN", "detail": "Underground SSBN pens; South Sea Fleet"},
    {"name": "Zhanjiang (South Sea Fleet)", "country": "CN", "host": "CN", "lat": 21.20, "lng": 110.40, "type": "naval_base", "branch": "PLAN", "detail": "South Sea Fleet HQ"},
    {"name": "Qingdao (North Sea Fleet)", "country": "CN", "host": "CN", "lat": 36.07, "lng": 120.38, "type": "naval_base", "branch": "PLAN", "detail": "North Sea Fleet HQ; CV-16 Liaoning"},
    {"name": "Djibouti Support Base", "country": "CN", "host": "DJ", "lat": 11.59, "lng": 43.05, "type": "military_base", "branch": "PLA", "detail": "China's first overseas military base (2017)"},
    {"name": "Fiery Cross Reef", "country": "CN", "host": "SCS", "lat": 9.55, "lng": 112.89, "type": "air_base", "branch": "PLA", "detail": "Artificial island; 3km runway; radar/SAM"},
    {"name": "Mischief Reef", "country": "CN", "host": "SCS", "lat": 9.90, "lng": 115.53, "type": "air_base", "branch": "PLA", "detail": "Artificial island; military facilities"},
    {"name": "Subi Reef", "country": "CN", "host": "SCS", "lat": 10.92, "lng": 114.08, "type": "air_base", "branch": "PLA", "detail": "Artificial island; military facilities"},
    {"name": "Jiuquan Launch Center", "country": "CN", "host": "CN", "lat": 40.96, "lng": 100.29, "type": "space_base", "branch": "PLA SSF", "detail": "Crewed space launches; military satellite launches"},
    # ── UK Major ──
    {"name": "HMNB Clyde (Faslane)", "country": "GB", "host": "GB", "lat": 56.07, "lng": -4.82, "type": "naval_base", "branch": "RN", "detail": "Vanguard-class SSBN home; UK nuclear deterrent"},
    {"name": "RAF Lakenheath", "country": "US", "host": "GB", "lat": 52.41, "lng": 0.56, "type": "air_base", "branch": "USAF", "detail": "48th FW; F-15E/F-35A; B61 nuclear weapons"},
    {"name": "Akrotiri (Cyprus)", "country": "GB", "host": "CY", "lat": 34.59, "lng": 32.99, "type": "air_base", "branch": "RAF", "detail": "Sovereign Base Area; ME operations hub"},
    # ── France Major ──
    {"name": "Île Longue", "country": "FR", "host": "FR", "lat": 48.30, "lng": -4.52, "type": "naval_base", "branch": "Marine", "detail": "SSBN base; Le Triomphant-class"},
    {"name": "Toulon Naval Base", "country": "FR", "host": "FR", "lat": 43.10, "lng": 5.94, "type": "naval_base", "branch": "Marine", "detail": "French Navy HQ; Charles de Gaulle CVN"},
    {"name": "Djibouti (FFDj)", "country": "FR", "host": "DJ", "lat": 11.55, "lng": 43.12, "type": "military_base", "branch": "French Armed Forces", "detail": "1,500 troops; largest French overseas base"},
    # ── NATO Training ──
    {"name": "Grafenwöhr Training Area", "country": "US", "host": "DE", "lat": 49.69, "lng": 11.93, "type": "training", "branch": "NATO", "detail": "Largest US Army training area in Europe"},
    {"name": "JMRC Hohenfels", "country": "US", "host": "DE", "lat": 49.06, "lng": 11.87, "type": "training", "branch": "NATO", "detail": "Joint Multinational Readiness Center"},
    {"name": "NATO JFTC Bydgoszcz", "country": "NATO", "host": "PL", "lat": 53.12, "lng": 18.00, "type": "training", "branch": "NATO", "detail": "Joint Force Training Centre"},
    # ── Other Notable ──
    {"name": "Pine Gap", "country": "AU", "host": "AU", "lat": -23.80, "lng": 133.74, "type": "intelligence", "branch": "Five Eyes", "detail": "Signals intelligence; satellite ground station"},
    {"name": "Changi Naval Base", "country": "SG", "host": "SG", "lat": 1.37, "lng": 104.00, "type": "naval_base", "branch": "RSN", "detail": "Key Indo-Pacific logistics hub"},
    {"name": "Al Dhafra Air Base", "country": "US", "host": "AE", "lat": 24.25, "lng": 54.55, "type": "air_base", "branch": "USAF", "detail": "F-22/F-35/U-2/RQ-4 operations"},
    {"name": "Anadyr (Chukotka)", "country": "RU", "host": "RU", "lat": 64.73, "lng": 177.51, "type": "air_base", "branch": "VKS", "detail": "Strategic bomber staging; Arctic defense"},
    {"name": "Cam Ranh Bay", "country": "VN", "host": "VN", "lat": 11.99, "lng": 109.22, "type": "naval_base", "branch": "VPN", "detail": "Former US/Soviet base; Vietnamese Navy HQ South"},

    # ── Germany (Bundeswehr) ──
    {"name": "Kommando Heer (Strausberg)", "country": "DE", "host": "DE", "lat": 52.58, "lng": 13.88, "type": "army_base", "branch": "Bundeswehr", "detail": "German Army Command HQ"},
    {"name": "Büchel Air Base", "country": "DE", "host": "DE", "lat": 50.17, "lng": 7.07, "type": "air_base", "branch": "Luftwaffe", "detail": "NATO nuclear sharing; Tornado IDS wing"},
    {"name": "Wilhelmshaven Naval Base", "country": "DE", "host": "DE", "lat": 53.51, "lng": 8.13, "type": "naval_base", "branch": "Deutsche Marine", "detail": "German Navy HQ; frigate homeport"},

    # ── Italy ──
    {"name": "Aviano Air Base", "country": "US", "host": "IT", "lat": 46.03, "lng": 12.60, "type": "air_base", "branch": "USAF", "detail": "31st FW; F-16 wing; NATO south flank"},
    {"name": "Naval Station Naples (Capodichino)", "country": "US", "host": "IT", "lat": 40.88, "lng": 14.29, "type": "naval_base", "branch": "USN", "detail": "HQ Allied Joint Force Command Naples; 6th Fleet"},
    {"name": "Taranto Naval Base", "country": "IT", "host": "IT", "lat": 40.47, "lng": 17.24, "type": "naval_base", "branch": "Marina Militare", "detail": "Italian Navy main base; Cavour homeport"},

    # ── Spain ──
    {"name": "Morón Air Base", "country": "US", "host": "ES", "lat": 37.18, "lng": -5.62, "type": "air_base", "branch": "USMC/USAF", "detail": "SPMAGTF-CR-AF staging; crisis response"},
    {"name": "Ferrol Naval Base", "country": "ES", "host": "ES", "lat": 43.48, "lng": -8.24, "type": "naval_base", "branch": "Armada Española", "detail": "Spanish Navy Atlantic base; F-100 frigates"},

    # ── Poland ──
    {"name": "Rzeszów-Jasionka (35th Air Wing)", "country": "PL", "host": "PL", "lat": 50.11, "lng": 22.02, "type": "air_base", "branch": "Polish AF", "detail": "NATO eastern flank hub; Ukraine logistics gateway"},
    {"name": "Redzikowo Aegis Ashore", "country": "US", "host": "PL", "lat": 54.48, "lng": 17.10, "type": "missile_defense", "branch": "USN/NATO", "detail": "Aegis Ashore BMD site; SM-3 interceptors"},
    {"name": "Poznań Land Command", "country": "PL", "host": "PL", "lat": 52.41, "lng": 16.93, "type": "army_base", "branch": "Polish Army", "detail": "HQ Polish Ground Forces"},

    # ── Norway ──
    {"name": "Bodø Main Air Station", "country": "NO", "host": "NO", "lat": 67.27, "lng": 14.37, "type": "air_base", "branch": "RNoAF", "detail": "NATO QRA North; arctic air operations"},
    {"name": "Ramsund Naval Station", "country": "NO", "host": "NO", "lat": 68.44, "lng": 16.52, "type": "naval_base", "branch": "RNoN", "detail": "Norwegian Navy northern base; coastal defense"},

    # ── Sweden ──
    {"name": "Luleå / Kallax Air Base (F 21)", "country": "SE", "host": "SE", "lat": 65.55, "lng": 22.13, "type": "air_base", "branch": "Flygvapnet", "detail": "Sweden's northernmost fighter wing; Gripen base"},
    {"name": "Berga Naval Base", "country": "SE", "host": "SE", "lat": 59.22, "lng": 18.21, "type": "naval_base", "branch": "Swedish Navy", "detail": "Swedish Navy main base; submarine flotilla"},

    # ── Finland ──
    {"name": "Rovaniemi Air Base (Lapland)", "country": "FI", "host": "FI", "lat": 66.56, "lng": 25.83, "type": "air_base", "branch": "Finnish AF", "detail": "NATO's newest Arctic air base; F/A-18 Hornets"},
    {"name": "Upinniemi Naval Base", "country": "FI", "host": "FI", "lat": 60.10, "lng": 24.36, "type": "naval_base", "branch": "Finnish Navy", "detail": "Finnish Navy main base; coastal defense"},

    # ── Denmark ──
    {"name": "Skrydstrup Air Base (Fighter Wing)", "country": "DK", "host": "DK", "lat": 55.22, "lng": 9.27, "type": "air_base", "branch": "RDAF", "detail": "Danish F-16/F-35 fighter wing; Baltic QRA"},
    {"name": "Frederikshavn Naval Station", "country": "DK", "host": "DK", "lat": 57.43, "lng": 10.54, "type": "naval_base", "branch": "Royal Danish Navy", "detail": "Danish Navy main operational base"},

    # ── Netherlands ──
    {"name": "Den Helder Naval Base", "country": "NL", "host": "NL", "lat": 52.96, "lng": 4.78, "type": "naval_base", "branch": "RNLN", "detail": "Royal Netherlands Navy main base; De Zeven Provinciën frigates"},
    {"name": "Volkel Air Base", "country": "NL", "host": "NL", "lat": 51.66, "lng": 5.71, "type": "air_base", "branch": "RNLAF", "detail": "NATO nuclear sharing; F-35A wing"},

    # ── Belgium ──
    {"name": "Kleine-Brogel Air Base", "country": "BE", "host": "BE", "lat": 51.17, "lng": 5.47, "type": "air_base", "branch": "Belgian AF", "detail": "NATO nuclear sharing; F-16 wing"},
    {"name": "Zeebrugge Naval Base", "country": "BE", "host": "BE", "lat": 51.34, "lng": 3.20, "type": "naval_base", "branch": "Belgian Navy", "detail": "Belgian naval component HQ; MCM vessels"},

    # ── Greece ──
    {"name": "Souda Bay (NSA Souda)", "country": "US", "host": "GR", "lat": 35.49, "lng": 24.12, "type": "naval_base", "branch": "USN/NATO", "detail": "Deep-water port; E Mediterranean logistics hub"},
    {"name": "Araxos Air Base", "country": "GR", "host": "GR", "lat": 38.15, "lng": 21.42, "type": "air_base", "branch": "Hellenic AF", "detail": "NATO storage; reserve fighter base"},

    # ── Turkey (domestic) ──
    {"name": "Çiğli Air Base (2nd Main Jet Base)", "country": "TR", "host": "TR", "lat": 38.51, "lng": 27.01, "type": "air_base", "branch": "TurAF", "detail": "Basic jet training; F-16 ops"},
    {"name": "Gölcük Naval Base", "country": "TR", "host": "TR", "lat": 40.72, "lng": 29.81, "type": "naval_base", "branch": "Turkish Navy", "detail": "Turkish Navy HQ; submarine force base"},
    {"name": "Akıncı Air Base (Ankara)", "country": "TR", "host": "TR", "lat": 40.08, "lng": 32.57, "type": "air_base", "branch": "TurAF", "detail": "F-16 wing; capital defense; Bayraktar ops"},

    # ── Romania ──
    {"name": "Mihail Kogălniceanu Air Base", "country": "US", "host": "RO", "lat": 44.36, "lng": 28.49, "type": "air_base", "branch": "USAF/NATO", "detail": "NATO eastern flank rotational hub; Black Sea ops"},
    {"name": "Deveselu Aegis Ashore", "country": "US", "host": "RO", "lat": 44.04, "lng": 24.28, "type": "missile_defense", "branch": "USN/NATO", "detail": "Aegis Ashore BMD site; SM-3 interceptors"},

    # ── Bulgaria ──
    {"name": "Graf Ignatievo Air Base", "country": "BG", "host": "BG", "lat": 42.29, "lng": 24.71, "type": "air_base", "branch": "Bulgarian AF", "detail": "Bulgarian fighter wing; MiG-29 → F-16 transition"},
    {"name": "Novo Selo Training Area", "country": "US", "host": "BG", "lat": 42.06, "lng": 25.60, "type": "training", "branch": "US Army/NATO", "detail": "Joint US-Bulgarian training range"},

    # ── Czech Republic ──
    {"name": "Čáslav Air Base (21st TAW)", "country": "CZ", "host": "CZ", "lat": 49.94, "lng": 15.38, "type": "air_base", "branch": "Czech AF", "detail": "JAS 39 Gripen wing; NATO QRA"},

    # ── Hungary ──
    {"name": "Kecskemét Air Base", "country": "HU", "host": "HU", "lat": 46.92, "lng": 19.74, "type": "air_base", "branch": "Hungarian AF", "detail": "JAS 39 Gripen wing; air policing"},

    # ── Portugal ──
    {"name": "Lajes Field (Azores)", "country": "US", "host": "PT", "lat": 38.76, "lng": -27.09, "type": "air_base", "branch": "USAF", "detail": "Mid-Atlantic staging; P-8 ASW ops"},

    # ── Estonia ──
    {"name": "Ämari Air Base", "country": "EE", "host": "EE", "lat": 59.26, "lng": 24.21, "type": "air_base", "branch": "Estonian AF/NATO", "detail": "NATO Baltic Air Policing rotational base"},
    {"name": "Tapa Military Base", "country": "EE", "host": "EE", "lat": 59.26, "lng": 25.96, "type": "army_base", "branch": "NATO eFP", "detail": "NATO enhanced Forward Presence battlegroup (UK-led)"},

    # ── Latvia ──
    {"name": "Lielvārde Air Base", "country": "LV", "host": "LV", "lat": 56.77, "lng": 24.85, "type": "air_base", "branch": "Latvian AF/NATO", "detail": "National Guard aviation; NATO rotational"},
    {"name": "Ādaži Military Base", "country": "LV", "host": "LV", "lat": 57.07, "lng": 24.33, "type": "army_base", "branch": "NATO eFP", "detail": "NATO enhanced Forward Presence battlegroup (Canada-led)"},

    # ── Lithuania ──
    {"name": "Šiauliai Air Base", "country": "LT", "host": "LT", "lat": 55.89, "lng": 23.39, "type": "air_base", "branch": "Lithuanian AF/NATO", "detail": "NATO Baltic Air Policing primary base"},
    {"name": "Rukla Military Base", "country": "LT", "host": "LT", "lat": 55.08, "lng": 24.02, "type": "army_base", "branch": "NATO eFP", "detail": "NATO enhanced Forward Presence battlegroup (Germany-led)"},

    # ── Croatia ──
    {"name": "Pleso Air Base (Zagreb)", "country": "HR", "host": "HR", "lat": 45.74, "lng": 16.07, "type": "air_base", "branch": "Croatian AF", "detail": "HQ Croatian Air Force; Rafale wing incoming"},

    # ── Slovakia ──
    {"name": "Sliač Air Base", "country": "SK", "host": "SK", "lat": 48.64, "lng": 19.14, "type": "air_base", "branch": "Slovak AF", "detail": "Slovak fighter wing; F-16 transition"},

    # ── Austria ──
    {"name": "Zeltweg Air Base (Hinterstoisser)", "country": "AT", "host": "AT", "lat": 47.21, "lng": 14.75, "type": "air_base", "branch": "Austrian AF", "detail": "Eurofighter Typhoon wing; Airpower show"},

    # ── Switzerland ──
    {"name": "Payerne Air Base", "country": "CH", "host": "CH", "lat": 46.84, "lng": 6.91, "type": "air_base", "branch": "Swiss AF", "detail": "F/A-18 Hornet wing; primary fighter base"},

    # ── Ireland ──
    {"name": "Casement Aerodrome (Baldonnel)", "country": "IE", "host": "IE", "lat": 53.30, "lng": -6.45, "type": "air_base", "branch": "Irish Air Corps", "detail": "Only Irish military airfield; maritime patrol"},

    # ── Israel ──
    {"name": "Nevatim Air Base", "country": "IL", "host": "IL", "lat": 31.21, "lng": 34.93, "type": "air_base", "branch": "IAF", "detail": "F-35I Adir wing; primary stealth operations"},
    {"name": "Palmachim Air Base", "country": "IL", "host": "IL", "lat": 31.90, "lng": 34.69, "type": "air_base", "branch": "IAF", "detail": "Missile test range; Arrow & Iron Dome batteries"},
    {"name": "Haifa Naval Base", "country": "IL", "host": "IL", "lat": 32.82, "lng": 34.98, "type": "naval_base", "branch": "Israeli Navy", "detail": "Israeli Navy HQ; Dolphin-class submarine base"},

    # ── Saudi Arabia ──
    {"name": "King Abdulaziz Air Base (Dhahran)", "country": "SA", "host": "SA", "lat": 26.27, "lng": 50.15, "type": "air_base", "branch": "RSAF", "detail": "Eastern Province air defense; F-15SA wing"},
    {"name": "Prince Sultan Air Base", "country": "US", "host": "SA", "lat": 24.07, "lng": 47.58, "type": "air_base", "branch": "USAF/RSAF", "detail": "CAOC fallback; F-22/F-15E rotational deployments"},
    {"name": "King Faisal Naval Base (Jeddah)", "country": "SA", "host": "SA", "lat": 21.52, "lng": 39.17, "type": "naval_base", "branch": "RSNF", "detail": "Red Sea fleet HQ; Western Fleet"},

    # ── UAE (domestic complement to Al Dhafra) ──
    {"name": "Zayed Military City", "country": "AE", "host": "AE", "lat": 24.37, "lng": 54.61, "type": "army_base", "branch": "UAE Armed Forces", "detail": "UAE Presidential Guard; ground forces HQ"},

    # ── Egypt ──
    {"name": "Cairo West Air Base", "country": "EG", "host": "EG", "lat": 30.12, "lng": 30.92, "type": "air_base", "branch": "EAF", "detail": "Egypt's primary military airfield; F-16/Rafale wing"},
    {"name": "Berenice Military Base", "country": "EG", "host": "EG", "lat": 23.97, "lng": 35.44, "type": "military_base", "branch": "Egyptian Armed Forces", "detail": "Red Sea mega-base; joint naval/air; opened 2020"},

    # ── Iran ──
    {"name": "Bandar Abbas Naval Base", "country": "IR", "host": "IR", "lat": 27.15, "lng": 56.28, "type": "naval_base", "branch": "IRIN/IRGCN", "detail": "Main Iranian naval base; Strait of Hormuz control"},
    {"name": "Isfahan Nuclear Technology Center", "country": "IR", "host": "IR", "lat": 32.63, "lng": 51.68, "type": "military_base", "branch": "IRGC", "detail": "UCF; nuclear fuel cycle facility; fortified"},
    {"name": "Bushehr (Khatam al-Anbiya garrison)", "country": "IR", "host": "IR", "lat": 28.95, "lng": 50.82, "type": "missile_defense", "branch": "IRGC-ASF", "detail": "Coastal defense; anti-ship missile batteries; near reactor"},

    # ── Iraq ──
    {"name": "Balad Air Base (Al Bakr)", "country": "IQ", "host": "IQ", "lat": 34.09, "lng": 44.36, "type": "air_base", "branch": "IqAF", "detail": "Iraq's largest airfield; F-16IQ wing; former USAF hub"},
    {"name": "Camp Taji (Al-Taji)", "country": "IQ", "host": "IQ", "lat": 33.54, "lng": 44.26, "type": "army_base", "branch": "Iraqi Army", "detail": "Iraqi armor/mechanized training; north of Baghdad"},

    # ── Jordan ──
    {"name": "Muwaffaq Salti Air Base (Al-Azraq)", "country": "JO", "host": "JO", "lat": 31.83, "lng": 36.78, "type": "air_base", "branch": "RJAF/USAF", "detail": "F-16 wing; US/coalition ISR operations hub"},

    # ── Oman ──
    {"name": "Thumrait Air Base", "country": "OM", "host": "OM", "lat": 17.67, "lng": 54.02, "type": "air_base", "branch": "RAFO/USAF", "detail": "Omani/US operations; major pre-positioned base"},
    {"name": "Duqm Naval Base", "country": "OM", "host": "OM", "lat": 19.66, "lng": 57.70, "type": "naval_base", "branch": "RNO/UK", "detail": "New deep-water facility; UK Joint Logistics Support Base"},

    # ── Kuwait ──
    {"name": "Ali Al Salem Air Base", "country": "US", "host": "KW", "lat": 29.35, "lng": 47.52, "type": "air_base", "branch": "USAF", "detail": "386th AEW; primary CENTCOM transit hub"},
    {"name": "Camp Arifjan", "country": "US", "host": "KW", "lat": 28.93, "lng": 48.10, "type": "army_base", "branch": "US Army", "detail": "HQ ARCENT forward; largest US Army base in ME"},

    # ── Bahrain (complement to NSA Bahrain) ──
    {"name": "Shaikh Isa Air Base", "country": "BH", "host": "BH", "lat": 25.92, "lng": 50.59, "type": "air_base", "branch": "RBAF", "detail": "Bahraini F-16 wing; joint operations"},

    # ── Qatar (complement to Al Udeid) ──
    {"name": "As Sayliyah Army Base", "country": "US", "host": "QA", "lat": 25.27, "lng": 51.39, "type": "army_base", "branch": "US Army", "detail": "Largest pre-positioned equipment stocks outside US"},

    # ── Lebanon ──
    {"name": "Beirut Air Base (Rafic Hariri)", "country": "LB", "host": "LB", "lat": 33.81, "lng": 35.49, "type": "air_base", "branch": "LAF", "detail": "Lebanese Armed Forces air wing; Super Tucano ops"},

    # ── Morocco ──
    {"name": "Kénitra Air Base", "country": "MA", "host": "MA", "lat": 34.30, "lng": -6.60, "type": "air_base", "branch": "Royal Moroccan AF", "detail": "F-16 wing; primary fighter base"},

    # ── Algeria ──
    {"name": "Tamanrasset Air Base", "country": "DZ", "host": "DZ", "lat": 22.81, "lng": 5.45, "type": "air_base", "branch": "Algerian AF", "detail": "Saharan defense; southern strategic projection"},
    {"name": "Mers El Kébir Naval Base", "country": "DZ", "host": "DZ", "lat": 35.73, "lng": -0.72, "type": "naval_base", "branch": "Algerian Navy", "detail": "Main naval base; Kilo-class submarine homeport"},

    # ── Tunisia ──
    {"name": "Bizerte Naval Base", "country": "TN", "host": "TN", "lat": 37.27, "lng": 9.87, "type": "naval_base", "branch": "Tunisian Navy", "detail": "Tunisian Navy HQ; Mediterranean patrol"},

    # ── Libya ──
    {"name": "Al-Jufra Air Base", "country": "LY", "host": "LY", "lat": 29.20, "lng": 16.00, "type": "air_base", "branch": "LNA", "detail": "Central Libya; contested; Russian Wagner/Africa Corps presence"},

    # ── Japan (domestic JSDF) ──
    {"name": "Yokosuka (JMSDF)", "country": "JP", "host": "JP", "lat": 35.29, "lng": 139.65, "type": "naval_base", "branch": "JMSDF", "detail": "JMSDF Fleet HQ; DDH Izumo homeport"},
    {"name": "Sasebo Naval Base (JMSDF)", "country": "JP", "host": "JP", "lat": 33.16, "lng": 129.72, "type": "naval_base", "branch": "JMSDF", "detail": "Amphibious force base; Osumi-class LPDs"},
    {"name": "Misawa Air Base", "country": "US", "host": "JP", "lat": 40.70, "lng": 141.37, "type": "air_base", "branch": "USAF/JASDF", "detail": "35th FW F-16s; SIGINT; northern Honshu"},

    # ── South Korea (domestic ROK) ──
    {"name": "Gyeryong (ROK MND HQ)", "country": "KR", "host": "KR", "lat": 36.27, "lng": 127.03, "type": "military_base", "branch": "ROK Armed Forces", "detail": "ROK Joint Chiefs/Service HQs; military capital"},
    {"name": "Jinhae Naval Base", "country": "KR", "host": "KR", "lat": 35.15, "lng": 128.68, "type": "naval_base", "branch": "ROKN", "detail": "ROK Navy HQ; submarine command"},

    # ── India ──
    {"name": "INS Kadamba (Karwar)", "country": "IN", "host": "IN", "lat": 14.80, "lng": 74.12, "type": "naval_base", "branch": "Indian Navy", "detail": "India's largest naval base; Arihant SSBN homeport"},
    {"name": "Ambala Air Force Station", "country": "IN", "host": "IN", "lat": 30.37, "lng": 76.82, "type": "air_base", "branch": "IAF", "detail": "Rafale wing; frontline base near Pakistan/China"},
    {"name": "Jodhpur Air Force Station", "country": "IN", "host": "IN", "lat": 26.25, "lng": 73.05, "type": "air_base", "branch": "IAF", "detail": "Su-30MKI wing; western sector defense"},
    {"name": "Port Blair (Andaman & Nicobar Command)", "country": "IN", "host": "IN", "lat": 11.65, "lng": 92.73, "type": "military_base", "branch": "Indian Tri-Service", "detail": "India's only tri-service command; Malacca Strait watch"},

    # ── Pakistan ──
    {"name": "PAC Kamra (Pakistan Aeronautical Complex)", "country": "PK", "host": "PK", "lat": 33.87, "lng": 72.40, "type": "air_base", "branch": "PAF", "detail": "JF-17 production; major PAF base"},
    {"name": "PNS Jinnah (Karachi Naval Dockyard)", "country": "PK", "host": "PK", "lat": 24.84, "lng": 66.98, "type": "naval_base", "branch": "Pakistan Navy", "detail": "Pakistan Navy HQ; submarine base"},
    {"name": "Sargodha Air Base (PAF Mushaf)", "country": "PK", "host": "PK", "lat": 32.05, "lng": 72.67, "type": "air_base", "branch": "PAF", "detail": "F-16/JF-17 wing; central air defense"},

    # ── Australia ──
    {"name": "HMAS Stirling (Fleet Base West)", "country": "AU", "host": "AU", "lat": -32.24, "lng": 115.69, "type": "naval_base", "branch": "RAN", "detail": "Indian Ocean fleet base; future AUKUS SSN homeport"},
    {"name": "RAAF Tindal", "country": "AU", "host": "AU", "lat": -14.52, "lng": 132.38, "type": "air_base", "branch": "RAAF", "detail": "Northern Australia; F-35A wing; USAF bomber rotations"},
    {"name": "RAAF Amberley", "country": "AU", "host": "AU", "lat": -27.64, "lng": 152.71, "type": "air_base", "branch": "RAAF", "detail": "F/A-18F Super Hornet / EA-18G Growler wing"},

    # ── New Zealand ──
    {"name": "Devonport Naval Base", "country": "NZ", "host": "NZ", "lat": -36.83, "lng": 174.80, "type": "naval_base", "branch": "RNZN", "detail": "Royal New Zealand Navy HQ; ANZAC frigate homeport"},
    {"name": "RNZAF Ohakea", "country": "NZ", "host": "NZ", "lat": -40.21, "lng": 175.39, "type": "air_base", "branch": "RNZAF", "detail": "RNZAF main operating base; NH90/A109 helicopters"},

    # ── Indonesia ──
    {"name": "Surabaya Naval Base (Koarmatim)", "country": "ID", "host": "ID", "lat": -7.25, "lng": 112.73, "type": "naval_base", "branch": "TNI-AL", "detail": "Eastern Fleet Command; largest Indonesian naval base"},
    {"name": "TNI HQ Cilangkap", "country": "ID", "host": "ID", "lat": -6.34, "lng": 106.89, "type": "military_base", "branch": "TNI", "detail": "Indonesian Armed Forces Headquarters"},

    # ── Thailand ──
    {"name": "U-Tapao Air Base", "country": "TH", "host": "TH", "lat": 12.68, "lng": 101.01, "type": "air_base", "branch": "RTAF/USN", "detail": "RTAF main base; Cobra Gold exercises; Gripen wing"},
    {"name": "Sattahip Naval Base", "country": "TH", "host": "TH", "lat": 12.67, "lng": 100.89, "type": "naval_base", "branch": "RTN", "detail": "Royal Thai Navy HQ; HTMS Chakri Naruebet homeport"},

    # ── Philippines ──
    {"name": "Subic Bay (Hanjin/Naval)", "country": "PH", "host": "PH", "lat": 14.80, "lng": 120.28, "type": "naval_base", "branch": "Philippine Navy", "detail": "Former US base; renewed EDCA site; SCS access"},
    {"name": "Clark Air Base (Basa)", "country": "PH", "host": "PH", "lat": 15.19, "lng": 120.56, "type": "air_base", "branch": "PAF", "detail": "EDCA enhanced cooperation site; FA-50 wing"},

    # ── Singapore (complement to Changi) ──
    {"name": "Tengah Air Base", "country": "SG", "host": "SG", "lat": 1.39, "lng": 103.71, "type": "air_base", "branch": "RSAF", "detail": "F-15SG/F-35B wing; Singapore's largest air base"},

    # ── Malaysia ──
    {"name": "RMAF Butterworth", "country": "MY", "host": "MY", "lat": 5.47, "lng": 100.39, "type": "air_base", "branch": "RMAF", "detail": "Su-30MKM/F/A-18D wing; Five Power Defence base"},
    {"name": "KD Sultan Abdul Halim (Kota Kinabalu)", "country": "MY", "host": "MY", "lat": 6.04, "lng": 116.05, "type": "naval_base", "branch": "RMN", "detail": "Eastern Fleet; South China Sea patrol"},

    # ── Myanmar ──
    {"name": "Naypyidaw (Ministry of Defence)", "country": "MM", "host": "MM", "lat": 19.76, "lng": 96.13, "type": "military_base", "branch": "Tatmadaw", "detail": "Myanmar military HQ; Tatmadaw command center"},

    # ── Taiwan ──
    {"name": "Zuoying Naval Base (Kaohsiung)", "country": "TW", "host": "TW", "lat": 22.70, "lng": 120.27, "type": "naval_base", "branch": "ROCN", "detail": "ROC Navy Fleet Command; submarine base"},
    {"name": "Hsinchu Air Base", "country": "TW", "host": "TW", "lat": 24.82, "lng": 120.94, "type": "air_base", "branch": "ROCAF", "detail": "Mirage 2000 wing; Taiwan Strait frontline"},
    {"name": "Hualien Air Base (Jiashan)", "country": "TW", "host": "TW", "lat": 24.02, "lng": 121.62, "type": "air_base", "branch": "ROCAF", "detail": "F-16V wing; underground mountain hangars"},

    # ── Bangladesh ──
    {"name": "BNS Haji Mohsin (Chattogram)", "country": "BD", "host": "BD", "lat": 22.30, "lng": 91.80, "type": "naval_base", "branch": "Bangladesh Navy", "detail": "Bangladesh Navy main base; Bay of Bengal patrol"},

    # ── Sri Lanka ──
    {"name": "SLNS Rangalla (Trincomalee)", "country": "LK", "host": "LK", "lat": 8.57, "lng": 81.23, "type": "naval_base", "branch": "Sri Lanka Navy", "detail": "Eastern naval command; deep-water harbor"},

    # ── Canada ──
    {"name": "CFB Esquimalt", "country": "CA", "host": "CA", "lat": 48.43, "lng": -123.42, "type": "naval_base", "branch": "RCN", "detail": "Pacific Fleet HQ; Victoria-class submarine base"},
    {"name": "CFB Cold Lake", "country": "CA", "host": "CA", "lat": 54.41, "lng": -110.28, "type": "air_base", "branch": "RCAF", "detail": "CF-18 Hornet wing; Maple Flag exercises"},
    {"name": "CFB Halifax", "country": "CA", "host": "CA", "lat": 44.63, "lng": -63.58, "type": "naval_base", "branch": "RCN", "detail": "Atlantic Fleet HQ; Halifax-class frigate homeport"},

    # ── Mexico ──
    {"name": "Campo Militar No. 1 (Mexico City)", "country": "MX", "host": "MX", "lat": 19.46, "lng": -99.21, "type": "army_base", "branch": "SEDENA", "detail": "Mexican Army HQ; largest military base in Mexico"},
    {"name": "Base Naval de Acapulco", "country": "MX", "host": "MX", "lat": 16.84, "lng": -99.92, "type": "naval_base", "branch": "SEMAR", "detail": "Pacific naval zone; counter-narcotics ops"},

    # ── Brazil ──
    {"name": "Brasília (1st Army Division)", "country": "BR", "host": "BR", "lat": -15.79, "lng": -47.88, "type": "army_base", "branch": "Brazilian Army", "detail": "Army HQ; capital garrison; strategic reserve"},
    {"name": "São Pedro da Aldeia (NAe)", "country": "BR", "host": "BR", "lat": -22.81, "lng": -42.09, "type": "naval_base", "branch": "Brazilian Navy", "detail": "Naval aviation base; NAe São Paulo operations"},
    {"name": "Manaus (CMA / 12th Military Region)", "country": "BR", "host": "BR", "lat": -3.13, "lng": -60.02, "type": "army_base", "branch": "Brazilian Army", "detail": "Amazon Military Command; jungle warfare center"},

    # ── Argentina ──
    {"name": "Puerto Belgrano Naval Base", "country": "AR", "host": "AR", "lat": -38.88, "lng": -62.08, "type": "naval_base", "branch": "ARA", "detail": "Argentine Navy main base; fleet HQ"},
    {"name": "Río Gallegos Air Base", "country": "AR", "host": "AR", "lat": -51.62, "lng": -69.31, "type": "air_base", "branch": "FAA", "detail": "Southern air defense; Patagonia/Falklands watch"},

    # ── Chile ──
    {"name": "Base Naval Talcahuano", "country": "CL", "host": "CL", "lat": -36.72, "lng": -73.11, "type": "naval_base", "branch": "Chilean Navy", "detail": "Chilean Navy main base; Scorpène submarine homeport"},
    {"name": "Punta Arenas (Chabunco Air Base)", "country": "CL", "host": "CL", "lat": -53.00, "lng": -70.85, "type": "air_base", "branch": "FACh", "detail": "Southernmost Chilean military base; Antarctic staging"},

    # ── Colombia ──
    {"name": "Palanquero Air Base (Germán Olano)", "country": "CO", "host": "CO", "lat": 5.48, "lng": -74.66, "type": "air_base", "branch": "FAC", "detail": "Colombian Air Force main base; Kfir wing"},
    {"name": "Tolemaida Military Base", "country": "CO", "host": "CO", "lat": 4.25, "lng": -74.65, "type": "army_base", "branch": "Colombian Army", "detail": "Largest military base in Colombia; rapid deployment force"},

    # ── Peru ──
    {"name": "Callao Naval Base (Base Naval del Callao)", "country": "PE", "host": "PE", "lat": -12.06, "lng": -77.16, "type": "naval_base", "branch": "Peruvian Navy", "detail": "Peruvian Navy HQ; Pacific fleet base"},
    {"name": "Las Palmas Air Base (Jorge Chávez)", "country": "PE", "host": "PE", "lat": -12.15, "lng": -76.99, "type": "air_base", "branch": "FAP", "detail": "Peruvian Air Force main base; MiG-29/Su-25 wing"},

    # ── Venezuela ──
    {"name": "Libertador Air Base (Palo Negro)", "country": "VE", "host": "VE", "lat": 10.18, "lng": -67.56, "type": "air_base", "branch": "AVB", "detail": "Venezuelan Air Force HQ; Su-30MKV wing"},
    {"name": "Puerto Cabello Naval Base", "country": "VE", "host": "VE", "lat": 10.47, "lng": -68.01, "type": "naval_base", "branch": "Venezuelan Navy", "detail": "Venezuelan Navy main base; patrol fleet HQ"},

    # ── Cuba ──
    {"name": "San Antonio de los Baños Air Base", "country": "CU", "host": "CU", "lat": 22.87, "lng": -82.51, "type": "air_base", "branch": "DAAFAR", "detail": "Cuban Air Force primary base; MiG-29 wing"},

    # ── Ecuador ──
    {"name": "Manta Air Base", "country": "EC", "host": "EC", "lat": -0.95, "lng": -80.68, "type": "air_base", "branch": "FAE", "detail": "Former US FOL; Ecuadorian AF Kfir/Super Tucano ops"},

    # ── South Africa ──
    {"name": "Simon's Town Naval Base", "country": "ZA", "host": "ZA", "lat": -34.19, "lng": 18.43, "type": "naval_base", "branch": "SAN", "detail": "South African Navy HQ; Cape sea-route patrol"},
    {"name": "AFB Hoedspruit", "country": "ZA", "host": "ZA", "lat": -24.37, "lng": 31.05, "type": "air_base", "branch": "SAAF", "detail": "Gripen wing; main fighter base"},

    # ── Nigeria ──
    {"name": "Abuja (Nigerian Armed Forces HQ)", "country": "NG", "host": "NG", "lat": 9.06, "lng": 7.49, "type": "military_base", "branch": "Nigerian Armed Forces", "detail": "Defence HQ; joint operations command"},
    {"name": "Port Harcourt Naval Base (NNS Pathfinder)", "country": "NG", "host": "NG", "lat": 4.78, "lng": 7.01, "type": "naval_base", "branch": "Nigerian Navy", "detail": "Niger Delta security; Gulf of Guinea patrol"},

    # ── Kenya ──
    {"name": "Nanyuki (British Army Training Unit)", "country": "GB", "host": "KE", "lat": 0.01, "lng": 37.07, "type": "training", "branch": "British Army", "detail": "BATUK; jungle/desert warfare training"},
    {"name": "Manda Bay (Camp Simba)", "country": "US", "host": "KE", "lat": -2.26, "lng": 40.91, "type": "military_base", "branch": "US DoD", "detail": "US operations base; counter-al-Shabaab staging"},

    # ── Ethiopia ──
    {"name": "Bishoftu (Debre Zeit) Air Base", "country": "ET", "host": "ET", "lat": 8.73, "lng": 38.95, "type": "air_base", "branch": "ENDF", "detail": "Ethiopian Air Force main base; Su-27/30 wing"},

    # ── DRC ──
    {"name": "Camp Kokolo (Kinshasa)", "country": "CD", "host": "CD", "lat": -4.33, "lng": 15.30, "type": "army_base", "branch": "FARDC", "detail": "DRC military garrison; Presidential Guard base"},

    # ── Cameroon ──
    {"name": "Douala Naval Base", "country": "CM", "host": "CM", "lat": 4.01, "lng": 9.73, "type": "naval_base", "branch": "Cameroon Navy", "detail": "Cameroon Navy HQ; Gulf of Guinea patrol"},

    # ── Somalia ──
    {"name": "Mogadishu (Halane Camp / Aden Abdulle)", "country": "SO", "host": "SO", "lat": 2.01, "lng": 45.30, "type": "military_base", "branch": "SNA/AMISOM", "detail": "Somali National Army HQ; AMISOM/ATMIS base"},

    # ── Sudan ──
    {"name": "Merowe Air Base", "country": "SD", "host": "SD", "lat": 18.44, "lng": 31.84, "type": "air_base", "branch": "SAF", "detail": "Sudanese AF forward base; MiG-29 ops"},

    # ── Ghana ──
    {"name": "Burma Camp (Accra)", "country": "GH", "host": "GH", "lat": 5.58, "lng": -0.16, "type": "army_base", "branch": "Ghana Armed Forces", "detail": "Ghana military HQ; major garrison"},

    # ── Senegal ──
    {"name": "Dakar-Ouakam Air Base", "country": "SN", "host": "SN", "lat": 14.73, "lng": -17.50, "type": "air_base", "branch": "Senegalese AF", "detail": "Senegal military air hub; Dakar garrison"},

    # ── Tanzania ──
    {"name": "Lugalo Military Camp (Dar es Salaam)", "country": "TZ", "host": "TZ", "lat": -6.81, "lng": 39.28, "type": "army_base", "branch": "TPDF", "detail": "Tanzania People's Defence Force HQ"},

    # ── Uganda ──
    {"name": "Entebbe Air Base", "country": "UG", "host": "UG", "lat": 0.05, "lng": 32.44, "type": "air_base", "branch": "UPDF", "detail": "UPDF Air Force main base; regional intervention staging"},

    # ── Rwanda ──
    {"name": "Kanombe Military Barracks (Kigali)", "country": "RW", "host": "RW", "lat": -1.97, "lng": 30.13, "type": "army_base", "branch": "RDF", "detail": "Rwanda Defence Force main garrison"},

    # ── Mozambique ──
    {"name": "Maputo Military Base", "country": "MZ", "host": "MZ", "lat": -25.97, "lng": 32.57, "type": "military_base", "branch": "FADM", "detail": "Mozambique Armed Forces HQ; southern command"},

    # ── Angola ──
    {"name": "Luanda Naval Base", "country": "AO", "host": "AO", "lat": -8.80, "lng": 13.23, "type": "naval_base", "branch": "Angolan Navy", "detail": "Angolan Navy HQ; offshore patrol"},

    # ── Zimbabwe ──
    {"name": "Thornhill Air Base (Gweru)", "country": "ZW", "host": "ZW", "lat": -19.44, "lng": 29.86, "type": "air_base", "branch": "AFZ", "detail": "Air Force of Zimbabwe main base; fighter/trainer wing"},

    # ── Kazakhstan ──
    {"name": "Baikonur Cosmodrome", "country": "RU", "host": "KZ", "lat": 45.62, "lng": 63.31, "type": "space_base", "branch": "Roscosmos", "detail": "World's first spaceport; leased by Russia; Soyuz launches"},

    # ── Uzbekistan ──
    {"name": "Termez Air Base", "country": "UZ", "host": "UZ", "lat": 37.24, "lng": 67.31, "type": "air_base", "branch": "Uzbek AF", "detail": "Southern border base; former coalition logistics hub"},

    # ── Turkmenistan ──
    {"name": "Mary Air Base", "country": "TM", "host": "TM", "lat": 37.62, "lng": 61.90, "type": "air_base", "branch": "Turkmen AF", "detail": "Turkmenistan's primary fighter base; MiG-29 wing"},

    # ── Mongolia ──
    {"name": "Buyant-Ukhaa (Five Hills Training Area)", "country": "MN", "host": "MN", "lat": 47.84, "lng": 106.77, "type": "training", "branch": "Mongolian AF", "detail": "Khaan Quest multinational exercises; main training area"},

    # ── North Korea ──
    {"name": "Pyongyang (Korean People's Army HQ)", "country": "KP", "host": "KP", "lat": 39.02, "lng": 125.75, "type": "military_base", "branch": "KPA", "detail": "Supreme Command; Ministry of People's Armed Forces"},
    {"name": "Yongbyon Nuclear Scientific Research Center", "country": "KP", "host": "KP", "lat": 39.80, "lng": 125.75, "type": "military_base", "branch": "KPA", "detail": "Primary nuclear weapons complex; 5MWe reactor; enrichment"},

    # ── Nepal ──
    {"name": "Bhadrakali Military Camp (Kathmandu)", "country": "NP", "host": "NP", "lat": 27.71, "lng": 85.32, "type": "army_base", "branch": "Nepal Army", "detail": "Nepal Army HQ; main garrison"},
]


# ═══════════════ MILITARY VESSEL POSITIONS ═══════════════
# Known carrier strike group and major surface vessel deployments (OSINT/public tracking)
# Approximate positions based on latest OSINT reporting
VESSEL_DEPLOYMENTS = [
    # ── US Navy Carrier Strike Groups ──
    {"name": "USS Harry S. Truman (CVN-75) CSG", "country": "US", "type": "carrier_strike_group", "lat": 26.5, "lng": 56.5, "heading": 90, "speed_kts": 12,
     "detail": "CSG-8 deployed to Arabian Sea/Gulf of Oman; Iran operations", "vessels": "CVN-75 + CG/DDGs + SSN"},
    {"name": "USS Carl Vinson (CVN-70) CSG", "country": "US", "type": "carrier_strike_group", "lat": 21.0, "lng": 128.0, "heading": 270, "speed_kts": 15,
     "detail": "CSG-1 deployed Western Pacific; Taiwan Strait patrols", "vessels": "CVN-70 + CG/DDGs + SSN"},
    {"name": "USS Theodore Roosevelt (CVN-71) CSG", "country": "US", "type": "carrier_strike_group", "lat": 13.0, "lng": 44.0, "heading": 180, "speed_kts": 10,
     "detail": "CSG-9 deployed Red Sea/Gulf of Aden; Houthi operations", "vessels": "CVN-71 + CG/DDGs + SSN"},
    {"name": "USS Ronald Reagan (CVN-76)", "country": "US", "type": "carrier", "lat": 35.3, "lng": 139.6, "heading": 0, "speed_kts": 0,
     "detail": "Forward-deployed at Yokosuka, Japan", "vessels": "CVN-76 (in port)"},
    {"name": "USS Gerald R. Ford (CVN-78)", "country": "US", "type": "carrier", "lat": 36.9, "lng": -76.3, "heading": 0, "speed_kts": 0,
     "detail": "In port Norfolk, VA; maintenance period", "vessels": "CVN-78 (in port)"},
    # ── US Navy ARGs ──
    {"name": "Bataan ARG / 26th MEU", "country": "US", "type": "amphibious_group", "lat": 33.5, "lng": 34.0, "heading": 180, "speed_kts": 8,
     "detail": "Deployed Eastern Mediterranean", "vessels": "LHD-5 + LPD + LSD + Marines"},
    # ── Royal Navy ──
    {"name": "HMS Queen Elizabeth (R08)", "country": "GB", "type": "carrier", "lat": 50.80, "lng": -1.11, "heading": 0, "speed_kts": 0,
     "detail": "Portsmouth; preparing for deployment", "vessels": "R08 + escorts"},
    {"name": "HMS Prince of Wales (R09)", "country": "GB", "type": "carrier", "lat": 56.46, "lng": -2.97, "heading": 0, "speed_kts": 0,
     "detail": "In port Rosyth; refit", "vessels": "R09"},
    # ── French Navy ──
    {"name": "Charles de Gaulle (R91) TF 473", "country": "FR", "type": "carrier_strike_group", "lat": 35.0, "lng": 18.0, "heading": 90, "speed_kts": 14,
     "detail": "Deployed Eastern Mediterranean; Clemenceau 26 mission", "vessels": "R91 + FREMM frigates + SSN"},
    # ── Chinese Navy ──
    {"name": "Shandong (CV-17) CSG", "country": "CN", "type": "carrier_strike_group", "lat": 17.5, "lng": 112.0, "heading": 180, "speed_kts": 12,
     "detail": "South China Sea patrol; combat readiness training", "vessels": "CV-17 + Type 055/052D destroyers"},
    {"name": "Fujian (CV-18)", "country": "CN", "type": "carrier", "lat": 31.35, "lng": 121.75, "heading": 0, "speed_kts": 0,
     "detail": "Sea trials from Shanghai; EMALS testing", "vessels": "CV-18 (sea trials)"},
    {"name": "Liaoning (CV-16)", "country": "CN", "type": "carrier", "lat": 36.0, "lng": 120.4, "heading": 0, "speed_kts": 0,
     "detail": "Qingdao homeport; training carrier", "vessels": "CV-16"},
    # ── Russian Navy ──
    {"name": "Admiral Kuznetsov", "country": "RU", "type": "carrier", "lat": 69.07, "lng": 33.42, "heading": 0, "speed_kts": 0,
     "detail": "Severomorsk; prolonged refit (limited operational capability)", "vessels": "Kuznetsov"},
    {"name": "Black Sea Fleet Surface Group", "country": "RU", "type": "surface_group", "lat": 44.6, "lng": 33.5, "heading": 180, "speed_kts": 8,
     "detail": "Reduced after Ukrainian strikes; frigates + patrol vessels", "vessels": "Frigates + corvettes"},
    {"name": "Pacific Fleet SSBN Patrol", "country": "RU", "type": "submarine", "lat": 52.0, "lng": 158.0, "heading": 0, "speed_kts": 6,
     "detail": "Borei-class SSBN deterrence patrol; Sea of Okhotsk bastion", "vessels": "SSBN (submerged)"},
    # ── Indian Navy ──
    {"name": "INS Vikrant (R11) CSG", "country": "IN", "type": "carrier_strike_group", "lat": 14.5, "lng": 72.0, "heading": 270, "speed_kts": 14,
     "detail": "Western Fleet; Arabian Sea patrol", "vessels": "R11 + Kolkata-class DDGs + frigates"},
    # ── Other Notable ──
    {"name": "JS Izumo (DDH-183)", "country": "JP", "type": "carrier", "lat": 35.29, "lng": 139.66, "heading": 0, "speed_kts": 0,
     "detail": "Yokosuka; F-35B conversion complete", "vessels": "DDH-183 (light carrier)"},
    {"name": "HMAS Canberra (L02)", "country": "AU", "type": "amphibious", "lat": -33.87, "lng": 151.21, "heading": 0, "speed_kts": 0,
     "detail": "Sydney; Indo-Pacific presence", "vessels": "L02 LHD"},
    {"name": "Cavour (C 550)", "country": "IT", "type": "carrier", "lat": 40.84, "lng": 14.27, "heading": 0, "speed_kts": 0,
     "detail": "Taranto homeport; F-35B capable", "vessels": "C 550"},

    # ── More US Navy ──
    {"name": "USS Nimitz (CVN-68)", "country": "US", "type": "carrier", "lat": 47.56, "lng": -122.63, "heading": 0, "speed_kts": 0, "detail": "Bremerton, WA; in port; maintenance", "vessels": "CVN-68 (in port)"},
    {"name": "USS Dwight D. Eisenhower (CVN-69)", "country": "US", "type": "carrier", "lat": 36.95, "lng": -76.33, "heading": 0, "speed_kts": 0, "detail": "Norfolk, VA; in port; post-deployment refit", "vessels": "CVN-69 (in port)"},
    {"name": "USS Abraham Lincoln (CVN-72) CSG", "country": "US", "type": "carrier_strike_group", "lat": 18.5, "lng": 134.0, "heading": 180, "speed_kts": 14, "detail": "Western Pacific/Philippine Sea; Indo-Pacific patrol", "vessels": "CVN-72 + CG/DDGs + SSN"},
    {"name": "USS John C. Stennis (CVN-74)", "country": "US", "type": "carrier", "lat": 36.95, "lng": -76.35, "heading": 0, "speed_kts": 0, "detail": "Norfolk; RCOH/refueling", "vessels": "CVN-74 (in port)"},
    {"name": "USS George Washington (CVN-73)", "country": "US", "type": "carrier", "lat": 35.28, "lng": 139.66, "heading": 0, "speed_kts": 0, "detail": "Forward-deployed Yokosuka (replacing Reagan)", "vessels": "CVN-73 (forward deployed)"},
    {"name": "USS Ohio (SSGN-726) SSBN patrol", "country": "US", "type": "submarine", "lat": 35.0, "lng": -140.0, "heading": 270, "speed_kts": 8, "detail": "Pacific; deterrence patrol", "vessels": "SSGN-726"},
    {"name": "USS West Virginia (SSBN-736)", "country": "US", "type": "submarine", "lat": 42.0, "lng": -52.0, "heading": 90, "speed_kts": 6, "detail": "Atlantic SSBN patrol", "vessels": "SSBN-736"},

    # ── South Korea ──
    {"name": "ROKS Dokdo (LPH-6111)", "country": "KR", "type": "amphibious", "lat": 35.08, "lng": 129.04, "heading": 0, "speed_kts": 0, "detail": "Busan; ROK Navy flagship", "vessels": "LPH-6111"},
    {"name": "ROKS Marado (LPH-6112)", "country": "KR", "type": "amphibious", "lat": 33.25, "lng": 126.57, "heading": 180, "speed_kts": 10, "detail": "Jeju patrol; Indo-Pacific patrol", "vessels": "LPH-6112"},
    {"name": "ROKS Sejong the Great (DDG-991)", "country": "KR", "type": "destroyer", "lat": 37.5, "lng": 131.5, "heading": 0, "speed_kts": 12, "detail": "East Sea patrol; Aegis destroyer", "vessels": "DDG-991"},

    # ── Turkey ──
    {"name": "TCG Anadolu (L-408)", "country": "TR", "type": "carrier", "lat": 38.42, "lng": 27.14, "heading": 270, "speed_kts": 0, "detail": "Izmir/Aegean; Turkey's first carrier (drone/F-35B capable)", "vessels": "L-408"},
    {"name": "TCG Istanbul (F-515)", "country": "TR", "type": "frigate", "lat": 35.5, "lng": 30.0, "heading": 90, "speed_kts": 14, "detail": "Eastern Mediterranean; MILGEM frigate", "vessels": "F-515"},

    # ── Egypt ──
    {"name": "ENS Gamal Abdel Nasser (L1010)", "country": "EG", "type": "amphibious", "lat": 31.20, "lng": 29.90, "heading": 0, "speed_kts": 0, "detail": "Alexandria; Mistral-class LHD", "vessels": "L1010"},
    {"name": "ENS Anwar El Sadat (L1020)", "country": "EG", "type": "amphibious", "lat": 24.0, "lng": 37.0, "heading": 180, "speed_kts": 12, "detail": "Red Sea patrol; Mistral-class LHD; Red Sea security", "vessels": "L1020"},

    # ── Spain ──
    {"name": "Juan Carlos I (L-61)", "country": "ES", "type": "carrier", "lat": 36.60, "lng": -6.38, "heading": 0, "speed_kts": 0, "detail": "Rota; Spanish Navy flagship; F-35B capable", "vessels": "L-61"},

    # ── Brazil ──
    {"name": "NAM Atlântico (A140)", "country": "BR", "type": "carrier", "lat": 22.90, "lng": -43.17, "heading": 0, "speed_kts": 0, "detail": "Rio de Janeiro; Ex-HMS Ocean; Brazilian Navy flagship", "vessels": "A140"},

    # ── Thailand ──
    {"name": "HTMS Chakri Naruebet", "country": "TH", "type": "carrier", "lat": 12.68, "lng": 100.88, "heading": 0, "speed_kts": 0, "detail": "Sattahip; Royal Thai Navy; world's smallest carrier", "vessels": "HTMS Chakri Naruebet"},

    # ── Germany ──
    {"name": "FGS Baden-Württemberg (F222)", "country": "DE", "type": "frigate", "lat": 55.5, "lng": 14.0, "heading": 90, "speed_kts": 12, "detail": "Baltic patrol; F125 frigate; NATO Baltic presence", "vessels": "F222"},
    {"name": "FGS Sachsen (F219)", "country": "DE", "type": "frigate", "lat": 54.5, "lng": 7.0, "heading": 0, "speed_kts": 0, "detail": "North Sea; Type 124 air defense frigate", "vessels": "F219"},

    # ── Netherlands ──
    {"name": "HNLMS Karel Doorman (A833)", "country": "NL", "type": "support_ship", "lat": 48.0, "lng": -15.0, "heading": 180, "speed_kts": 10, "detail": "Atlantic; Joint Support Ship; NATO logistics", "vessels": "A833"},

    # ── Greece ──
    {"name": "HS Hydra (F452)", "country": "GR", "type": "frigate", "lat": 37.5, "lng": 25.5, "heading": 90, "speed_kts": 14, "detail": "Aegean patrol; MEKO 200 frigate; Aegean presence", "vessels": "F452"},

    # ── Singapore ──
    {"name": "RSS Endurance (207)", "country": "SG", "type": "amphibious", "lat": 3.5, "lng": 105.0, "heading": 90, "speed_kts": 10, "detail": "South China Sea; Endurance-class LST", "vessels": "207"},

    # ── Indonesia ──
    {"name": "KRI Makassar (590)", "country": "ID", "type": "amphibious", "lat": -5.5, "lng": 112.0, "heading": 90, "speed_kts": 10, "detail": "Java Sea patrol; Makassar-class LPD; archipelago patrol", "vessels": "590"},

    # ── Iran ──
    {"name": "IRIS Makran (441)", "country": "IR", "type": "support_ship", "lat": 26.6, "lng": 56.3, "heading": 180, "speed_kts": 8, "detail": "Strait of Hormuz; Forward staging base; largest Iranian warship", "vessels": "441"},
    {"name": "IRIS Dena (75)", "country": "IR", "type": "frigate", "lat": 25.5, "lng": 58.0, "heading": 270, "speed_kts": 12, "detail": "Gulf of Oman; Moudge-class frigate; Gulf patrol", "vessels": "75"},

    # ── Pakistan ──
    {"name": "PNS Moawin (A39)", "country": "PK", "type": "support_ship", "lat": 24.0, "lng": 66.0, "heading": 180, "speed_kts": 10, "detail": "Arabian Sea; Fleet tanker; PN Task Group", "vessels": "A39"},
    {"name": "PNS Tughril (F263)", "country": "PK", "type": "frigate", "lat": 12.0, "lng": 65.0, "heading": 270, "speed_kts": 14, "detail": "Indian Ocean; Type 054A/P frigate from China", "vessels": "F263"},

    # ── Saudi Arabia ──
    {"name": "HMS Al Riyadh (812)", "country": "SA", "type": "frigate", "lat": 20.5, "lng": 38.5, "heading": 180, "speed_kts": 12, "detail": "Red Sea patrol; La Fayette-class; Red Sea security", "vessels": "812"},

    # ── Japan (additional) ──
    {"name": "JS Kaga (DDH-184)", "country": "JP", "type": "carrier", "lat": 28.0, "lng": 135.0, "heading": 180, "speed_kts": 14, "detail": "Western Pacific patrol; Izumo-class; F-35B capable; Pacific patrol", "vessels": "DDH-184"},
    {"name": "JS Maya (DDG-179)", "country": "JP", "type": "destroyer", "lat": 30.0, "lng": 128.5, "heading": 270, "speed_kts": 12, "detail": "East China Sea; Maya-class Aegis BMD", "vessels": "DDG-179"},

    # ── France (additional) ──
    {"name": "FS Mistral (L9013)", "country": "FR", "type": "amphibious", "lat": 10.0, "lng": 52.0, "heading": 90, "speed_kts": 12, "detail": "Indian Ocean; Mistral-class LHD; Indian Ocean deployment", "vessels": "L9013"},
    {"name": "FS Dixmude (L9015)", "country": "FR", "type": "amphibious", "lat": -4.0, "lng": 6.0, "heading": 180, "speed_kts": 10, "detail": "West Africa; Mistral-class; Gulf of Guinea patrol", "vessels": "L9015"},

    # ── UK (additional) ──
    {"name": "HMS Daring (D32)", "country": "GB", "type": "destroyer", "lat": 26.0, "lng": 52.0, "heading": 90, "speed_kts": 14, "detail": "Persian Gulf; Type 45 destroyer; Gulf patrol", "vessels": "D32"},
    {"name": "HMS Spey (P234)", "country": "GB", "type": "patrol", "lat": 1.0, "lng": 115.0, "heading": 90, "speed_kts": 12, "detail": "Indo-Pacific; River-class OPV; Indo-Pacific deployment", "vessels": "P234"},

    # ── Canada ──
    {"name": "HMCS Harry DeWolf (AOPV-430)", "country": "CA", "type": "patrol", "lat": 72.0, "lng": -90.0, "heading": 0, "speed_kts": 8, "detail": "Arctic; Arctic OPV; Northwest Passage", "vessels": "AOPV-430"},

    # ── Norway ──
    {"name": "KNM Fridtjof Nansen (F310)", "country": "NO", "type": "frigate", "lat": 67.0, "lng": 10.0, "heading": 0, "speed_kts": 12, "detail": "Norwegian Sea; Nansen-class; NATO Northern Flank", "vessels": "F310"},

    # ── Denmark ──
    {"name": "HDMS Absalon (L16)", "country": "DK", "type": "frigate", "lat": 55.0, "lng": 12.0, "heading": 90, "speed_kts": 10, "detail": "Baltic Sea; Absalon-class support ship", "vessels": "L16"},

    # ── Sweden ──
    {"name": "HMS Gotland (Gtd)", "country": "SE", "type": "submarine", "lat": 58.5, "lng": 18.0, "heading": 0, "speed_kts": 6, "detail": "Baltic Sea; Gotland-class AIP submarine; Baltic ASW", "vessels": "Gtd"},

    # ── Italy (additional) ──
    {"name": "ITS Trieste (L9890)", "country": "IT", "type": "carrier", "lat": 37.0, "lng": 16.0, "heading": 180, "speed_kts": 10, "detail": "Central Mediterranean; LHD; Italy's largest warship; F-35B capable", "vessels": "L9890"},

    # ── Russia (additional) ──
    {"name": "Admiral Gorshkov SAG", "country": "RU", "type": "surface_group", "lat": 36.0, "lng": -8.0, "heading": 90, "speed_kts": 12, "detail": "Atlantic/Mediterranean; Gorshkov-class frigate + tanker; Zircon hypersonic missiles", "vessels": "Gorshkov SAG"},
    {"name": "Northern Fleet SSBN Patrol", "country": "RU", "type": "submarine", "lat": 72.0, "lng": 38.0, "heading": 0, "speed_kts": 5, "detail": "Barents Sea; Delta IV SSBN; strategic deterrence", "vessels": "Delta IV SSBN"},

    # ── China (additional) ──
    {"name": "Type 075 Hainan (31)", "country": "CN", "type": "amphibious", "lat": 16.0, "lng": 110.5, "heading": 180, "speed_kts": 12, "detail": "South China Sea; Type 075 LHD; amphibious assault ship", "vessels": "31"},
    {"name": "Type 075 Guangxi (32)", "country": "CN", "type": "amphibious", "lat": 29.0, "lng": 124.0, "heading": 90, "speed_kts": 10, "detail": "East China Sea; Type 075 LHD; Taiwan contingency readiness", "vessels": "32"},
    {"name": "PLAN Southern Theater SSN", "country": "CN", "type": "submarine", "lat": 14.0, "lng": 115.0, "heading": 180, "speed_kts": 8, "detail": "South China Sea deep patrol; Type 093 SSN; SCS patrol", "vessels": "Type 093 SSN"},

    # ── India (additional) ──
    {"name": "INS Vikramaditya (R33)", "country": "IN", "type": "carrier", "lat": 13.0, "lng": 84.0, "heading": 90, "speed_kts": 14, "detail": "Eastern Fleet Bay of Bengal; Modified Kiev-class; Eastern Fleet flagship", "vessels": "R33"},
    {"name": "INS Arihant (S73)", "country": "IN", "type": "submarine", "lat": 10.0, "lng": 82.0, "heading": 180, "speed_kts": 6, "detail": "Bay of Bengal SSBN patrol; Arihant-class SSBN; India's sea-based nuclear deterrent", "vessels": "S73"},

    # ── Latin America ──
    {"name": "ARA Almirante Irízar Q-5", "country": "AR", "type": "surface_group", "lat": -55.0, "lng": -65.0, "heading": 180, "speed_kts": 12, "detail": "South Atlantic / Antarctic; icebreaker + ARA Bouchard; Antarctic patrol", "vessels": "Q-5 + Bouchard"},
    {"name": "ARA Espora SAG", "country": "AR", "type": "surface_group", "lat": -38.0, "lng": -57.0, "heading": 90, "speed_kts": 14, "detail": "Argentine EEZ patrol; MEKO 140 corvettes; rebuilding fleet", "vessels": "MEKO 140 corvettes"},
    {"name": "Almirante Cochrane FFG-05", "country": "CL", "type": "surface_group", "lat": -33.0, "lng": -72.0, "heading": 270, "speed_kts": 16, "detail": "Pacific Chilean EEZ; Type 23 frigate (ex-RN); fleet flagship", "vessels": "FFG-05"},
    {"name": "Almirante Williams FF-19", "country": "CL", "type": "surface_group", "lat": -53.0, "lng": -71.0, "heading": 0, "speed_kts": 12, "detail": "Drake Passage / Magellan Strait; Type 22 frigate (ex-RN)", "vessels": "FF-19"},
    {"name": "ARC 7 de Agosto FM-53", "country": "CO", "type": "surface_group", "lat": 11.5, "lng": -73.0, "heading": 90, "speed_kts": 14, "detail": "Caribbean counter-narcotics; Almirante Padilla-class corvette", "vessels": "FM-53"},
    {"name": "ARC Pacific Patrol", "country": "CO", "type": "surface_group", "lat": 4.0, "lng": -78.0, "heading": 180, "speed_kts": 12, "detail": "Pacific counter-narcotics; OPV-80 + Riohacha-class", "vessels": "Pacific OPV"},
    {"name": "ARM Reformador POLA-101", "country": "MX", "type": "surface_group", "lat": 22.0, "lng": -97.0, "heading": 90, "speed_kts": 18, "detail": "Gulf of Mexico; long-range patrol; Damen Sigma 10514", "vessels": "POLA-101"},
    {"name": "BNS Maranhão", "country": "BR", "type": "amphibious", "lat": -22.0, "lng": -42.0, "heading": 180, "speed_kts": 14, "detail": "Brazilian Atlantic; Bahia-class LPD (ex-French Foudre)", "vessels": "G40"},
    {"name": "BAP Almirante Grau (CLM-81)", "country": "PE", "type": "surface_group", "lat": -12.0, "lng": -77.5, "heading": 270, "speed_kts": 14, "detail": "Pacific Peruvian EEZ; Lupo-class frigates; ex-Italian", "vessels": "Lupo frigates"},

    # ── Southeast Asia / Pacific ──
    {"name": "RTN HTMS Chakri Naruebet (911)", "country": "TH", "type": "carrier", "lat": 12.0, "lng": 100.5, "heading": 180, "speed_kts": 8, "detail": "Gulf of Thailand; smallest carrier in service; helicopter ops only", "vessels": "911"},
    {"name": "TNI-AL Diponegoro Sigma-class", "country": "ID", "type": "surface_group", "lat": -3.0, "lng": 116.0, "heading": 90, "speed_kts": 16, "detail": "Java Sea; Sigma 9113 corvettes; ASEAN patrol", "vessels": "365-368"},
    {"name": "TNI-AL KRI Nagapasa-class SSK", "country": "ID", "type": "submarine", "lat": -8.0, "lng": 115.0, "heading": 90, "speed_kts": 8, "detail": "Lombok Strait SLOC; Type 209/1400 South Korean-built", "vessels": "Nagapasa SSK"},
    {"name": "RSN Formidable-class FFG", "country": "SG", "type": "surface_group", "lat": 1.2, "lng": 104.0, "heading": 90, "speed_kts": 18, "detail": "Singapore Strait; La Fayette stealth frigate", "vessels": "Formidable FFG"},
    {"name": "RSN Invincible-class SSK", "country": "SG", "type": "submarine", "lat": 1.0, "lng": 105.0, "heading": 90, "speed_kts": 8, "detail": "Singapore Strait; Type 218SG; AIP-equipped", "vessels": "Invincible SSK"},
    {"name": "PLAN Type 075 Anhui (33)", "country": "CN", "type": "amphibious", "lat": 22.0, "lng": 118.0, "heading": 0, "speed_kts": 14, "detail": "Taiwan Strait; third Type 075 LHD; rapid amphibious build", "vessels": "33"},
    {"name": "PLAN Type 071 Yuzhao LPD", "country": "CN", "type": "amphibious", "lat": 18.0, "lng": 110.0, "heading": 180, "speed_kts": 14, "detail": "Hainan; 8x Type 071 LPDs; amphibious lift", "vessels": "Type 071 LPD"},

    # ── Middle East / North Africa ──
    {"name": "MM Mohammed VI FREMM", "country": "MA", "type": "surface_group", "lat": 33.5, "lng": -7.5, "heading": 270, "speed_kts": 18, "detail": "Atlantic Morocco; FREMM frigate; most capable in N Africa", "vessels": "Mohammed VI"},
    {"name": "Algerian Navy Kalaat-class LPD", "country": "DZ", "type": "amphibious", "lat": 36.5, "lng": 3.0, "heading": 90, "speed_kts": 14, "detail": "Mediterranean; San Giorgio-class LPD; Russian/Chinese mix fleet", "vessels": "Kalaat Beni Abbes"},
    {"name": "Algerian Kilo-class SSK", "country": "DZ", "type": "submarine", "lat": 36.0, "lng": 5.0, "heading": 0, "speed_kts": 6, "detail": "Western Mediterranean; 6x Kilo SSK fleet; largest sub force in Africa", "vessels": "Kilo SSK"},
    {"name": "Iranian IRIS Makran", "country": "IR", "type": "surface_group", "lat": 25.0, "lng": 56.0, "heading": 90, "speed_kts": 12, "detail": "Strait of Hormuz; converted oil tanker forward base; helicopter mothership", "vessels": "Makran"},
    {"name": "Iranian IRGC Boats Hormuz", "country": "IR", "type": "surface_group", "lat": 26.5, "lng": 56.5, "heading": 0, "speed_kts": 30, "detail": "Strait of Hormuz; swarm boats + anti-ship cruise missiles", "vessels": "IRGC FAC"},

    # ── Africa ──
    {"name": "SAS Spioenkop F147", "country": "ZA", "type": "surface_group", "lat": -34.0, "lng": 18.0, "heading": 270, "speed_kts": 16, "detail": "Cape of Good Hope; Valour-class frigate (MEKO A-200); SLOC chokepoint", "vessels": "F147"},
    {"name": "Nigerian NNS Unity", "country": "NG", "type": "surface_group", "lat": 6.0, "lng": 4.0, "heading": 270, "speed_kts": 14, "detail": "Gulf of Guinea anti-piracy; ex-USCG Hamilton-class cutter", "vessels": "F92"},

    # ── Europe (additional) ──
    {"name": "FS Suffren SSN", "country": "FR", "type": "submarine", "lat": 38.0, "lng": 5.0, "heading": 90, "speed_kts": 8, "detail": "Western Mediterranean; Barracuda-class SSN; cruise missile capable", "vessels": "Suffren"},
    {"name": "FS Triomphant SSBN Patrol", "country": "FR", "type": "submarine", "lat": 48.0, "lng": -10.0, "heading": 270, "speed_kts": 6, "detail": "Bay of Biscay; Force océanique stratégique SSBN; M51 SLBMs", "vessels": "Triomphant SSBN"},
    {"name": "HMS Astute SSN", "country": "GB", "type": "submarine", "lat": 60.0, "lng": -5.0, "heading": 0, "speed_kts": 10, "detail": "GIUK Gap; Astute-class SSN; Tomahawk-capable", "vessels": "Astute SSN"},
    {"name": "HMS Vigilant SSBN", "country": "GB", "type": "submarine", "lat": 56.0, "lng": -8.0, "heading": 270, "speed_kts": 5, "detail": "North Atlantic CASD; Vanguard-class SSBN; Trident D5", "vessels": "Vigilant"},
    {"name": "FGS Bayern F217", "country": "DE", "type": "surface_group", "lat": 5.0, "lng": 95.0, "heading": 90, "speed_kts": 16, "detail": "Indian Ocean Indo-Pacific deployment; Brandenburg-class; FONOP", "vessels": "F217"},
    {"name": "ESPS Méndez Núñez F104", "country": "ES", "type": "surface_group", "lat": 40.0, "lng": 2.0, "heading": 270, "speed_kts": 18, "detail": "Western Mediterranean; F100 Álvaro de Bazán-class AAW; SM-2 capable", "vessels": "F104"},
    {"name": "HNoMS Maud (A530)", "country": "NO", "type": "surface_group", "lat": 70.0, "lng": 25.0, "heading": 90, "speed_kts": 14, "detail": "Norwegian Sea; logistics replenishment; NATO Northern Flank", "vessels": "A530"},
    {"name": "HMS Polish Orkan FFG", "country": "PL", "type": "surface_group", "lat": 54.5, "lng": 18.5, "heading": 0, "speed_kts": 18, "detail": "Baltic Sea; Tarantul-class FAC; Saab RBS-15 anti-ship", "vessels": "Orkan"},
    {"name": "ROS Mărăşeşti F111", "country": "RO", "type": "surface_group", "lat": 44.0, "lng": 30.0, "heading": 90, "speed_kts": 14, "detail": "Black Sea Romanian EEZ; Mărăşeşti frigate; only Romanian-built capital ship", "vessels": "F111"},
    {"name": "Bulgarian Drazki F41", "country": "BG", "type": "surface_group", "lat": 43.0, "lng": 28.5, "heading": 90, "speed_kts": 14, "detail": "Black Sea; Wielingen-class frigate (ex-Belgian); NATO BSF", "vessels": "F41"},
    {"name": "Ukrainian Magura V5 USV swarm", "country": "UA", "type": "surface_group", "lat": 44.5, "lng": 33.0, "heading": 180, "speed_kts": 35, "detail": "Black Sea; uncrewed surface drones; sank/damaged Russian Black Sea Fleet vessels", "vessels": "Magura V5 USV"},

    # ── North America (additional) ──
    {"name": "HMCS Halifax-class Atlantic", "country": "CA", "type": "surface_group", "lat": 45.0, "lng": -55.0, "heading": 90, "speed_kts": 16, "detail": "North Atlantic NATO SNMG; Halifax-class FFH; CSC replacement coming", "vessels": "Halifax FFH"},
    {"name": "HMCS Victoria SSK", "country": "CA", "type": "submarine", "lat": 48.0, "lng": -125.0, "heading": 270, "speed_kts": 8, "detail": "Eastern Pacific; ex-RN Upholder-class; aging fleet"},

    # ── Asia (additional) ──
    {"name": "JS Kaga DDH-184", "country": "JP", "type": "carrier", "lat": 32.0, "lng": 132.0, "heading": 180, "speed_kts": 14, "detail": "East China Sea; Izumo-class converted to F-35B carrier", "vessels": "DDH-184"},
    {"name": "JS Sōryū SSK", "country": "JP", "type": "submarine", "lat": 30.0, "lng": 130.0, "heading": 90, "speed_kts": 8, "detail": "East China Sea; Soryu-class AIP SSK; world-class conventional sub", "vessels": "Sōryū"},
    {"name": "ROKS Dokdo LPH-6111", "country": "KR", "type": "amphibious", "lat": 35.0, "lng": 130.0, "heading": 0, "speed_kts": 14, "detail": "Sea of Japan; Dokdo-class LPH; named after disputed islets", "vessels": "LPH-6111"},
    {"name": "ROKS Dosan Ahn Changho SS-083", "country": "KR", "type": "submarine", "lat": 35.0, "lng": 129.0, "heading": 0, "speed_kts": 8, "detail": "Sea of Japan; KSS-III AIP SSK; SLBM-capable (K-SLBM)", "vessels": "SS-083"},
    {"name": "VPN Gepard 3.9 frigate", "country": "VN", "type": "surface_group", "lat": 12.0, "lng": 110.0, "heading": 0, "speed_kts": 16, "detail": "South China Sea / Spratlys; Russian Gepard; coastal defense", "vessels": "Gepard 3.9"},
    {"name": "VPN Kilo 636 SSK", "country": "VN", "type": "submarine", "lat": 11.0, "lng": 109.0, "heading": 90, "speed_kts": 6, "detail": "South China Sea; 6x Kilo SSK; A2/AD against PLAN", "vessels": "Kilo 636"},
    {"name": "PNS Agosta-90B SSK", "country": "PK", "type": "submarine", "lat": 24.0, "lng": 65.0, "heading": 90, "speed_kts": 8, "detail": "Arabian Sea; French Agosta + Hangor-class (Chinese) coming online", "vessels": "Agosta-90B SSK"},
    {"name": "BNS Mongla F112", "country": "BD", "type": "surface_group", "lat": 22.0, "lng": 91.5, "heading": 180, "speed_kts": 14, "detail": "Bay of Bengal; Type 053H3 frigate (ex-Chinese); EEZ patrol", "vessels": "F112"},
    {"name": "Sri Lanka Navy SLNS Sayurala", "country": "LK", "type": "surface_group", "lat": 7.5, "lng": 79.5, "heading": 0, "speed_kts": 14, "detail": "Indian Ocean SLOC; Sayurala-class OPV; counter-piracy", "vessels": "SLNS Sayurala"},
    {"name": "Myanmar Navy Aung Zeya FF-1", "country": "MM", "type": "surface_group", "lat": 16.0, "lng": 96.0, "heading": 270, "speed_kts": 14, "detail": "Andaman Sea; Aung Zeya frigate; junta-controlled", "vessels": "FF-1"},
]


# ═══════════════ OPENSKY AIRCRAFT TRACKING ═══════════════
OPENSKY_CACHE = {"data": [], "fetched_at": 0.0}
OPENSKY_CACHE_TTL = 30  # 30 seconds

def fetch_opensky():
    """Fetch live aircraft from OpenSky Network API. Returns list of aircraft dicts."""
    now = time.time()
    if OPENSKY_CACHE["data"] and (now - OPENSKY_CACHE["fetched_at"]) < OPENSKY_CACHE_TTL:
        return OPENSKY_CACHE["data"]
    results = []
    try:
        url = "https://opensky-network.org/api/states/all"
        req = urllib.request.Request(url, headers={"User-Agent": "WorldMonitor/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read())
        states = raw.get("states", [])
        # Each state: [icao24, callsign, origin_country, time_pos, last_contact,
        #              lng, lat, baro_alt, on_ground, velocity, heading, vert_rate,
        #              sensors, geo_alt, squawk, spi, pos_source, category]
        for s in states:
            if s[6] is None or s[5] is None:
                continue  # no position
            if s[8]:
                continue  # on ground
            # Category: 0=N/A, 1=No info, 2=Light, 3=Small, 4=Large,
            # 5=High vortex, 6=Heavy, 7=High perf, 8=Rotorcraft, 9=Glider,
            # 10=Lighter-than-air, 11=Parachute, 12=Ultralight, 14=UAV,
            # 15=Space, 16=Surface emergency, 17=Surface service
            cat = s[17] if len(s) > 17 else 0
            cat_names = {0:"Unknown",1:"N/A",2:"Light",3:"Small",4:"Large",5:"High Vortex Large",
                         6:"Heavy",7:"High Performance",8:"Rotorcraft",9:"Glider",10:"Lighter-than-air",
                         14:"UAV/Drone",15:"Space"}
            results.append({
                "icao": s[0],
                "callsign": (s[1] or "").strip(),
                "origin": s[2] or "",
                "lat": round(s[6], 3),
                "lng": round(s[5], 3),
                "alt_m": int(s[7] or s[13] or 0),
                "alt_ft": int((s[7] or s[13] or 0) * 3.281),
                "speed_kts": int((s[9] or 0) * 1.944),
                "heading": int(s[10] or 0),
                "vert_rate": round(s[11] or 0, 1),
                "squawk": s[14] or "",
                "type_desc": cat_names.get(cat, ""),
                "on_ground": s[8],
            })
        with _cache_lock:
            OPENSKY_CACHE["data"] = results
            OPENSKY_CACHE["fetched_at"] = now
    except Exception as e:
        print(f"[OpenSky] Error: {e}")
        # Return cache even if stale
    return OPENSKY_CACHE["data"]


# Known military callsign prefixes
_MIL_PREFIXES = {
    "RCH", "REACH", "FORTE", "JAKE", "DUKE", "DOOM", "VIPER", "TIGER",
    "DEMON", "HAWK", "EAGLE", "SNTRY", "NCHO", "TOPCT", "IRON", "STORK",
    "COBRA", "VAPOR", "TEAL", "ATLAS", "DAGGER", "REAPER", "GHOST",
    "HYPER", "ORDER", "STEEL", "EVAC", "MOOSE", "POLAR", "HAVE",
    "ARROW", "BOLT", "FURY", "LANCE", "SABER", "TANGO", "WOLFP",
    "CHAOS", "NIGHT", "STORM", "RAID", "SCOUT", "FLASH", "GRID",
    "RRR", "CASA", "NAVY", "ARMY", "USAF", "AIRMIL", "NATO", "GAF",
    "CNV", "BAF", "IAF", "RAF", "FAF", "PAF", "VMFA", "VMGR",
    "BDOG", "PELCN", "STNGR", "HKYNS", "QUID", "GOTHAM", "OTIS",
}
# Military ICAO hex ranges (start, end) — known allocations for military transponders
_MIL_ICAO_RANGES = [
    (0xAE0000, 0xAEF2AF),  # US military
    (0xADF7C8, 0xADFFFF),  # US military (additional)
    (0x43C000, 0x43CFFF),  # UK military
    (0x3A8000, 0x3AFFFF),  # France military
    (0x3F4000, 0x3F7FFF),  # Germany military
    (0x300000, 0x303FFF),  # Italy military
    (0x340100, 0x340FFF),  # Spain military
    (0x480000, 0x480FFF),  # Netherlands military
    (0x710000, 0x710FFF),  # Australia military
    (0xC2C000, 0xC2CFFF),  # Canada military
    (0x7CF800, 0x7CFFFF),  # Japan military
    (0x501000, 0x501FFF),  # Israel military
    (0x0D0000, 0x0D7FFF),  # India military
    (0xE40000, 0xE40FFF),  # Brazil military
    (0x738000, 0x738FFF),  # South Korea military
    (0x800200, 0x8002FF),  # Turkey military
    (0x510000, 0x510FFF),  # Saudi Arabia military
]


def _is_mil_icao(icao_hex: str) -> bool:
    """Check if ICAO hex falls in a known military range."""
    try:
        val = int(icao_hex, 16)
        return any(lo <= val <= hi for lo, hi in _MIL_ICAO_RANGES)
    except (ValueError, TypeError):
        return False


def filter_military_aircraft(all_aircraft):
    """Return only positively-identified military/government aircraft."""
    results = []
    for a in all_aircraft:
        cs = a["callsign"].upper()
        # Known military callsign prefix
        if cs and any(cs.startswith(p) for p in _MIL_PREFIXES):
            results.append(a)
            continue
        # ICAO hex in a known military range
        if _is_mil_icao(a.get("icao", "")):
            results.append(a)
            continue
    return results


# ═══════════════ NASA EONET – NATURAL DISASTERS ═══════════════
EONET_CACHE = {"data": [], "fetched_at": 0.0}
EONET_CACHE_TTL = 300  # 5 minutes

def fetch_eonet():
    now = time.time()
    if EONET_CACHE["data"] and (now - EONET_CACHE["fetched_at"]) < EONET_CACHE_TTL:
        return EONET_CACHE["data"]
    results = []
    try:
        url = "https://eonet.gsfc.nasa.gov/api/v3/events?status=open&limit=200"
        req = urllib.request.Request(url, headers={"User-Agent": "WorldMonitor/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read())
        for ev in raw.get("events", []):
            cats = [c.get("title", "") for c in ev.get("categories", [])]
            cat = cats[0] if cats else "Unknown"
            geom = ev.get("geometry", [])
            if not geom:
                continue
            latest = geom[-1]
            coords = latest.get("coordinates", [])
            if len(coords) < 2:
                continue
            results.append({
                "id": ev.get("id", ""),
                "title": ev.get("title", ""),
                "category": cat,
                "lat": round(coords[1], 3),
                "lng": round(coords[0], 3),
                "date": latest.get("date", ""),
                "magnitude": latest.get("magnitudeValue"),
                "magnitude_unit": latest.get("magnitudeUnit", ""),
                "link": ev.get("link", ""),
            })
        with _cache_lock:
            EONET_CACHE["data"] = results
            EONET_CACHE["fetched_at"] = now
    except Exception as e:
        print(f"[EONET] Error: {e}")
    return EONET_CACHE["data"]


# ═══════════════ CELESTRAK – SATELLITE TRACKING ═══════════════
SATELLITE_CACHE = {"data": [], "fetched_at": 0.0}
SATELLITE_CACHE_TTL = 600  # 10 minutes (TLEs don't change fast)

def _sat_position(s, now_dt):
    """Compute approximate lat/lng/alt from Keplerian orbital elements."""
    try:
        epoch_str = s.get("EPOCH", "")
        if not epoch_str:
            return None
        epoch_dt = datetime.fromisoformat(epoch_str.replace("Z", "+00:00"))
        if epoch_dt.tzinfo is None:
            epoch_dt = epoch_dt.replace(tzinfo=timezone.utc)
        dt_sec = (now_dt - epoch_dt).total_seconds()

        inc = math.radians(s.get("INCLINATION", 0))
        raan = math.radians(s.get("RA_OF_ASC_NODE", 0))
        argp = math.radians(s.get("ARG_OF_PERICENTER", 0))
        M0 = math.radians(s.get("MEAN_ANOMALY", 0))
        n = s.get("MEAN_MOTION", 0)  # revs/day
        if n <= 0:
            return None
        ecc = s.get("ECCENTRICITY", 0)

        # Mean anomaly at current time
        M = M0 + 2 * math.pi * n * dt_sec / 86400.0
        # Solve Kepler's equation (1 iteration for low eccentricity)
        E = M + ecc * math.sin(M)
        # True anomaly
        nu = 2 * math.atan2(
            math.sqrt(1 + ecc) * math.sin(E / 2),
            math.sqrt(1 - ecc) * math.cos(E / 2),
        )
        u = argp + nu  # argument of latitude

        # Geodetic latitude
        lat = math.degrees(math.asin(math.sin(inc) * math.sin(u)))

        # GMST: approximate sidereal time
        # J2000 epoch = 2000-01-01T12:00:00 UTC
        j2000 = datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        d = (now_dt - j2000).total_seconds() / 86400.0
        gmst = math.radians((280.46061837 + 360.98564736629 * d) % 360)

        # Longitude
        lon_eci = raan + math.atan2(math.cos(inc) * math.sin(u), math.cos(u))
        lng = math.degrees(lon_eci - gmst)
        lng = ((lng + 180) % 360) - 180

        # Altitude (km) from semi-major axis
        mu = 398600.4418  # km^3/s^2
        a = (mu / (n * 2 * math.pi / 86400.0) ** 2) ** (1.0 / 3.0)
        alt_km = a - 6371.0

        return round(lat, 2), round(lng, 2), round(alt_km, 0)
    except Exception:
        return None


def fetch_satellites():
    """Fetch satellite GP data from CelesTrak, compute positions server-side."""
    now = time.time()
    if SATELLITE_CACHE["data"] and (now - SATELLITE_CACHE["fetched_at"]) < SATELLITE_CACHE_TTL:
        return SATELLITE_CACHE["data"]
    results = []
    now_dt = datetime.now(timezone.utc)
    groups = [
        ("military", "https://celestrak.org/NORAD/elements/gp.php?GROUP=military&FORMAT=json"),
        ("gps", "https://celestrak.org/NORAD/elements/gp.php?GROUP=gps-ops&FORMAT=json"),
        ("stations", "https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=json"),
        ("geo", "https://celestrak.org/NORAD/elements/gp.php?GROUP=geo&FORMAT=json"),
        ("weather", "https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=json"),
        ("earth-obs", "https://celestrak.org/NORAD/elements/gp.php?GROUP=resource&FORMAT=json"),
        ("comms", "https://celestrak.org/NORAD/elements/gp.php?GROUP=intelsat&FORMAT=json"),
        ("science", "https://celestrak.org/NORAD/elements/gp.php?GROUP=science&FORMAT=json"),
        ("starlink", "https://celestrak.org/NORAD/elements/gp.php?GROUP=starlink&FORMAT=json"),
    ]
    for group_name, url in groups:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "WorldMonitor/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                sats = json.loads(resp.read())
            for s in sats:
                pos = _sat_position(s, now_dt)
                if not pos:
                    continue
                results.append({
                    "name": s.get("OBJECT_NAME", ""),
                    "norad_id": s.get("NORAD_CAT_ID", 0),
                    "group": group_name,
                    "lat": pos[0],
                    "lng": pos[1],
                    "alt_km": pos[2],
                })
        except Exception as e:
            print(f"[CelesTrak:{group_name}] Error: {e}")
    with _cache_lock:
        SATELLITE_CACHE["data"] = results
        SATELLITE_CACHE["fetched_at"] = now
    print(f"[CelesTrak] Computed positions for {len(results)} satellites")
    return SATELLITE_CACHE["data"]


# ═══════════════ USGS – LIVE EARTHQUAKES ═══════════════
EARTHQUAKE_CACHE = {"data": [], "fetched_at": 0.0}
EARTHQUAKE_CACHE_TTL = 300  # 5 minutes

def fetch_earthquakes():
    now = time.time()
    if EARTHQUAKE_CACHE["data"] and (now - EARTHQUAKE_CACHE["fetched_at"]) < EARTHQUAKE_CACHE_TTL:
        return EARTHQUAKE_CACHE["data"]
    results = []
    try:
        # USGS feed: M2.5+ in past day (free, no key)
        url = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"
        req = urllib.request.Request(url, headers={"User-Agent": "WorldMonitor/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read())
        for feat in raw.get("features", []):
            props = feat.get("properties", {})
            geom = feat.get("geometry", {})
            coords = geom.get("coordinates", [])
            if len(coords) < 2:
                continue
            mag = props.get("mag")
            if mag is None or mag < 2.5:
                continue
            results.append({
                "id": feat.get("id", ""),
                "place": props.get("place", "")[:80],
                "mag": round(float(mag), 1),
                "depth_km": round(float(coords[2] or 0), 1) if len(coords) > 2 else 0,
                "lat": round(coords[1], 3),
                "lng": round(coords[0], 3),
                "time": props.get("time", 0),
                "tsunami": props.get("tsunami", 0) == 1,
                "felt": props.get("felt") or 0,
                "alert": props.get("alert", ""),
                "url": props.get("url", ""),
            })
        results.sort(key=lambda x: x["mag"], reverse=True)
        with _cache_lock:
            EARTHQUAKE_CACHE["data"] = results
            EARTHQUAKE_CACHE["fetched_at"] = now
        print(f"[USGS] Fetched {len(results)} earthquakes (M2.5+)")
    except Exception as e:
        print(f"[USGS] Error: {e}")
    return EARTHQUAKE_CACHE["data"]


# ═══════════════ NASA FIRMS – ACTIVE WILDFIRES ═══════════════
WILDFIRE_CACHE = {"data": [], "fetched_at": 0.0}
WILDFIRE_CACHE_TTL = 1800  # 30 minutes

def fetch_wildfires():
    now = time.time()
    if WILDFIRE_CACHE["data"] and (now - WILDFIRE_CACHE["fetched_at"]) < WILDFIRE_CACHE_TTL:
        return WILDFIRE_CACHE["data"]
    results = []
    try:
        # NASA FIRMS: VIIRS_SNPP_NRT, last 24h, global. CSV format.
        # Free, no key required for public 24h feed via firms.modaps.eosdis.nasa.gov
        url = "https://firms.modaps.eosdis.nasa.gov/data/active_fire/suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_Global_24h.csv"
        req = urllib.request.Request(url, headers={"User-Agent": "WorldMonitor/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
        lines = text.strip().split("\n")
        if len(lines) < 2:
            return []
        # Header: latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight
        # Confidence values: "high" / "nominal" / "low"
        sampled = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) < 13:
                continue
            try:
                lat = float(parts[0])
                lng = float(parts[1])
                conf = parts[8].strip().lower()
                frp = float(parts[11]) if parts[11] else 0  # Fire Radiative Power
                # Keep high-confidence + nominal-confidence fires with FRP >= 20 MW
                if conf not in ("high", "nominal") or frp < 20:
                    continue
                sampled.append({
                    "lat": round(lat, 3),
                    "lng": round(lng, 3),
                    "frp": round(frp, 1),
                    "brightness": round(float(parts[2]), 1) if parts[2] else 0,
                    "date": parts[5],
                    "time": parts[6],
                    "daynight": parts[12].strip() if len(parts) > 12 else "",
                })
            except (ValueError, IndexError):
                continue
        # Cap at 500 hottest
        sampled.sort(key=lambda x: x["frp"], reverse=True)
        results = sampled[:500]
        with _cache_lock:
            WILDFIRE_CACHE["data"] = results
            WILDFIRE_CACHE["fetched_at"] = now
        print(f"[FIRMS] Fetched {len(results)} active wildfires (high confidence, FRP>30)")
    except Exception as e:
        print(f"[FIRMS] Error: {e}")
    return WILDFIRE_CACHE["data"]


# ═══════════════ STRATEGIC CHOKEPOINTS ═══════════════
# Critical maritime/land chokepoints — bottlenecks for global trade and military movement
STRATEGIC_CHOKEPOINTS = [
    {"name": "Strait of Hormuz", "lat": 26.57, "lng": 56.25, "type": "maritime", "country": "IR/OM",
     "throughput": "21 Mbpd oil (~21% world supply)", "risk": "EXTREME", "detail": "Iran threatens closure during conflicts; US 5th Fleet patrols"},
    {"name": "Suez Canal", "lat": 30.59, "lng": 32.27, "type": "maritime", "country": "EG",
     "throughput": "12% global trade; 30% container traffic", "risk": "HIGH", "detail": "Houthi threats to Red Sea since 2023; Ever Given grounding"},
    {"name": "Strait of Malacca", "lat": 2.50, "lng": 101.50, "type": "maritime", "country": "SG/MY/ID",
     "throughput": "16 Mbpd oil; 25% world trade", "risk": "HIGH", "detail": "China's 'Malacca Dilemma'; piracy hotspot"},
    {"name": "Bab el-Mandeb", "lat": 12.58, "lng": 43.33, "type": "maritime", "country": "YE/DJ",
     "throughput": "9% seaborne oil trade; Red Sea entrance", "risk": "EXTREME", "detail": "Houthi missile/drone attacks ongoing; vessels rerouting Cape route"},
    {"name": "Bosphorus Strait", "lat": 41.12, "lng": 29.07, "type": "maritime", "country": "TR",
     "throughput": "3% world oil; only Black Sea access", "risk": "HIGH", "detail": "Montreux Convention; Russian Black Sea Fleet access"},
    {"name": "Strait of Gibraltar", "lat": 35.97, "lng": -5.50, "type": "maritime", "country": "ES/MA",
     "throughput": "Mediterranean entrance; 100k+ ships/yr", "risk": "MEDIUM", "detail": "NATO controls; UK base at Gibraltar"},
    {"name": "Panama Canal", "lat": 9.08, "lng": -79.68, "type": "maritime", "country": "PA",
     "throughput": "5% global trade; 14k ships/yr", "risk": "MEDIUM", "detail": "Drought-induced capacity restrictions 2024; US-China tensions over ports"},
    {"name": "Strait of Dover", "lat": 51.00, "lng": 1.50, "type": "maritime", "country": "GB/FR",
     "throughput": "Busiest shipping lane; 400+ ships/day", "risk": "LOW", "detail": "Channel Tunnel; English Channel"},
    {"name": "Strait of Magellan", "lat": -53.80, "lng": -70.95, "type": "maritime", "country": "CL/AR",
     "throughput": "Cape Horn alternative; Antarctic gateway", "risk": "LOW", "detail": "Strategic in Cape Horn rerouting scenarios"},
    {"name": "Singapore Strait", "lat": 1.27, "lng": 104.00, "type": "maritime", "country": "SG/MY/ID",
     "throughput": "Eastern Malacca; 1000+ ships/day", "risk": "MEDIUM", "detail": "Piracy; container hub Singapore"},
    {"name": "Taiwan Strait", "lat": 24.50, "lng": 119.50, "type": "maritime", "country": "TW/CN",
     "throughput": "50% global container traffic", "risk": "EXTREME", "detail": "PLA naval/air drills; US FONOPs"},
    {"name": "Luzon Strait", "lat": 20.50, "lng": 121.00, "type": "maritime", "country": "PH/TW",
     "throughput": "South China Sea ↔ Pacific", "risk": "HIGH", "detail": "Critical for US Pacific access; Bashi Channel"},
    {"name": "Lombok Strait", "lat": -8.50, "lng": 115.85, "type": "maritime", "country": "ID",
     "throughput": "Deep alternative to Malacca", "risk": "LOW", "detail": "Used by VLCCs too deep for Malacca"},
    {"name": "Sunda Strait", "lat": -6.00, "lng": 105.85, "type": "maritime", "country": "ID",
     "throughput": "Java↔Sumatra; alternative passage", "risk": "LOW", "detail": "Anak Krakatoa volcanic risk"},
    {"name": "Strait of Tiran", "lat": 27.97, "lng": 34.50, "type": "maritime", "country": "EG/SA",
     "throughput": "Gulf of Aqaba access; Eilat/Aqaba", "risk": "HIGH", "detail": "Saudi-Egypt tunnel project; Israeli access"},
    {"name": "Kerch Strait", "lat": 45.30, "lng": 36.55, "type": "maritime", "country": "RU/UA",
     "throughput": "Sea of Azov access", "risk": "EXTREME", "detail": "Crimea bridge; Ukrainian strikes; closed to Ukraine"},
    {"name": "Danish Straits", "lat": 56.00, "lng": 11.00, "type": "maritime", "country": "DK/SE",
     "throughput": "Baltic-North Sea; Russian oil exports", "risk": "MEDIUM", "detail": "Nord Stream area; sanctions enforcement"},
    {"name": "Northwest Passage", "lat": 74.00, "lng": -95.00, "type": "maritime", "country": "CA",
     "throughput": "Arctic shortcut (seasonal)", "risk": "MEDIUM", "detail": "Climate change opening; Canada/US dispute"},
    {"name": "Northern Sea Route", "lat": 76.00, "lng": 100.00, "type": "maritime", "country": "RU",
     "throughput": "Russia Arctic; China-EU shortcut", "risk": "MEDIUM", "detail": "Russia controls; nuclear icebreakers"},
    {"name": "Khyber Pass", "lat": 34.10, "lng": 71.10, "type": "land", "country": "PK/AF",
     "throughput": "Pakistan-Afghanistan land route", "risk": "HIGH", "detail": "Historic invasion route; Taliban control"},
    {"name": "Wakhan Corridor", "lat": 37.10, "lng": 73.50, "type": "land", "country": "AF/CN",
     "throughput": "Afghanistan↔China only land link", "risk": "HIGH", "detail": "Belt and Road; Pamir mountains"},
]


# ═══════════════ STOCK EXCHANGES & FINANCIAL CENTERS ═══════════════
STOCK_EXCHANGES = [
    {"name": "NYSE", "city": "New York", "country": "US", "lat": 40.71, "lng": -74.01, "mcap": "$28.4T", "tier": 1, "detail": "World's largest by market cap"},
    {"name": "NASDAQ", "city": "New York", "country": "US", "lat": 40.76, "lng": -73.99, "mcap": "$22.5T", "tier": 1, "detail": "Tech-heavy; AAPL, MSFT, NVDA"},
    {"name": "Shanghai SE", "city": "Shanghai", "country": "CN", "lat": 31.23, "lng": 121.50, "mcap": "$7.4T", "tier": 1, "detail": "China A-shares; SSE Composite"},
    {"name": "Euronext", "city": "Amsterdam", "country": "NL", "lat": 52.37, "lng": 4.89, "mcap": "$6.5T", "tier": 1, "detail": "Pan-European exchange (Paris/Amsterdam/Brussels/Lisbon/Milan/Oslo/Dublin)"},
    {"name": "Japan Exchange", "city": "Tokyo", "country": "JP", "lat": 35.68, "lng": 139.77, "mcap": "$6.2T", "tier": 1, "detail": "Nikkei 225; TOPIX"},
    {"name": "Shenzhen SE", "city": "Shenzhen", "country": "CN", "lat": 22.54, "lng": 114.05, "mcap": "$4.8T", "tier": 1, "detail": "China growth/tech stocks; ChiNext"},
    {"name": "Hong Kong Ex", "city": "Hong Kong", "country": "HK", "lat": 22.28, "lng": 114.16, "mcap": "$4.5T", "tier": 1, "detail": "Hang Seng; China gateway"},
    {"name": "LSE", "city": "London", "country": "GB", "lat": 51.51, "lng": -0.10, "mcap": "$3.4T", "tier": 1, "detail": "FTSE 100; oldest major exchange"},
    {"name": "TSX", "city": "Toronto", "country": "CA", "lat": 43.65, "lng": -79.38, "mcap": "$3.1T", "tier": 2, "detail": "Resource-heavy; mining/energy"},
    {"name": "NSE India", "city": "Mumbai", "country": "IN", "lat": 19.06, "lng": 72.86, "mcap": "$4.6T", "tier": 1, "detail": "Nifty 50; world's most active by volume"},
    {"name": "BSE India", "city": "Mumbai", "country": "IN", "lat": 18.93, "lng": 72.83, "mcap": "$4.7T", "tier": 1, "detail": "Sensex; oldest in Asia"},
    {"name": "Saudi Tadawul", "city": "Riyadh", "country": "SA", "lat": 24.71, "lng": 46.67, "mcap": "$2.7T", "tier": 2, "detail": "Aramco listed; largest in MENA"},
    {"name": "Deutsche Börse", "city": "Frankfurt", "country": "DE", "lat": 50.11, "lng": 8.67, "mcap": "$2.3T", "tier": 2, "detail": "DAX 40; Xetra trading"},
    {"name": "SIX Swiss Ex", "city": "Zurich", "country": "CH", "lat": 47.38, "lng": 8.54, "mcap": "$2.2T", "tier": 2, "detail": "SMI; Nestle, Roche, Novartis"},
    {"name": "Korea Exchange", "city": "Seoul", "country": "KR", "lat": 37.51, "lng": 127.06, "mcap": "$2.1T", "tier": 2, "detail": "KOSPI; Samsung dominant"},
    {"name": "ASX", "city": "Sydney", "country": "AU", "lat": -33.87, "lng": 151.21, "mcap": "$1.8T", "tier": 2, "detail": "ASX 200; mining/banking heavy"},
    {"name": "Taiwan SE", "city": "Taipei", "country": "TW", "lat": 25.04, "lng": 121.51, "mcap": "$2.3T", "tier": 2, "detail": "TAIEX; TSMC dominant"},
    {"name": "Brasil B3", "city": "São Paulo", "country": "BR", "lat": -23.55, "lng": -46.63, "mcap": "$0.9T", "tier": 2, "detail": "Bovespa; Latam largest"},
    {"name": "Johannesburg SE", "city": "Johannesburg", "country": "ZA", "lat": -26.10, "lng": 28.05, "mcap": "$1.0T", "tier": 2, "detail": "JSE; African mining; Naspers"},
    {"name": "Moscow Exchange", "city": "Moscow", "country": "RU", "lat": 55.75, "lng": 37.62, "mcap": "$0.5T", "tier": 3, "detail": "MOEX; sanctioned; reduced foreign access"},
    {"name": "Borsa İstanbul", "city": "Istanbul", "country": "TR", "lat": 41.08, "lng": 29.02, "mcap": "$0.4T", "tier": 3, "detail": "BIST 100; high inflation volatility"},
    {"name": "Tel Aviv SE", "city": "Tel Aviv", "country": "IL", "lat": 32.07, "lng": 34.79, "mcap": "$0.3T", "tier": 3, "detail": "TA-35; tech/defense"},
    {"name": "Singapore Ex", "city": "Singapore", "country": "SG", "lat": 1.28, "lng": 103.85, "mcap": "$0.6T", "tier": 2, "detail": "STI; ASEAN financial hub"},
    {"name": "DFM Dubai", "city": "Dubai", "country": "AE", "lat": 25.22, "lng": 55.28, "mcap": "$0.2T", "tier": 3, "detail": "DFM; Middle East commerce hub"},
    {"name": "QSE Doha", "city": "Doha", "country": "QA", "lat": 25.30, "lng": 51.53, "mcap": "$0.16T", "tier": 3, "detail": "Qatar Stock Exchange"},
]

CENTRAL_BANKS = [
    {"name": "Federal Reserve", "city": "Washington D.C.", "country": "US", "lat": 38.89, "lng": -77.04, "rate": "5.25-5.50%", "currency": "USD", "detail": "Sets global benchmark; FOMC"},
    {"name": "ECB", "city": "Frankfurt", "country": "DE", "lat": 50.11, "lng": 8.68, "rate": "4.00%", "currency": "EUR", "detail": "Eurozone 20 members"},
    {"name": "Bank of England", "city": "London", "country": "GB", "lat": 51.51, "lng": -0.09, "rate": "5.00%", "currency": "GBP", "detail": "Oldest central bank (1694)"},
    {"name": "Bank of Japan", "city": "Tokyo", "country": "JP", "lat": 35.68, "lng": 139.77, "rate": "0.25%", "currency": "JPY", "detail": "Exited NIRP 2024; YCC ended"},
    {"name": "PBOC", "city": "Beijing", "country": "CN", "lat": 39.92, "lng": 116.39, "rate": "3.10% LPR", "currency": "CNY", "detail": "People's Bank of China; MLF rate"},
    {"name": "SNB", "city": "Bern", "country": "CH", "lat": 46.95, "lng": 7.44, "rate": "1.00%", "currency": "CHF", "detail": "Swiss National Bank"},
    {"name": "Bank of Canada", "city": "Ottawa", "country": "CA", "lat": 45.42, "lng": -75.70, "rate": "3.75%", "currency": "CAD", "detail": "BoC; cutting cycle 2024"},
    {"name": "RBA", "city": "Sydney", "country": "AU", "lat": -33.87, "lng": 151.21, "rate": "4.35%", "currency": "AUD", "detail": "Reserve Bank of Australia"},
    {"name": "RBI India", "city": "Mumbai", "country": "IN", "lat": 18.93, "lng": 72.84, "rate": "6.50%", "currency": "INR", "detail": "Reserve Bank of India"},
    {"name": "BCB Brazil", "city": "Brasília", "country": "BR", "lat": -15.78, "lng": -47.93, "rate": "11.25%", "currency": "BRL", "detail": "Banco Central do Brasil; Selic"},
    {"name": "CBR Russia", "city": "Moscow", "country": "RU", "lat": 55.75, "lng": 37.62, "rate": "21.00%", "currency": "RUB", "detail": "Hiking aggressively; war economy"},
    {"name": "Turkey CBRT", "city": "Ankara", "country": "TR", "lat": 39.93, "lng": 32.86, "rate": "50.00%", "currency": "TRY", "detail": "Anti-inflation tightening 2024"},
    {"name": "BoK Korea", "city": "Seoul", "country": "KR", "lat": 37.57, "lng": 126.98, "rate": "3.25%", "currency": "KRW", "detail": "Bank of Korea"},
    {"name": "SAMA", "city": "Riyadh", "country": "SA", "lat": 24.71, "lng": 46.68, "rate": "5.50%", "currency": "SAR", "detail": "Saudi Central Bank; USD peg"},
]


# ═══════════════ MINING SITES & CRITICAL MINERALS ═══════════════
MINING_SITES = [
    # Lithium
    {"name": "Salar de Atacama", "country": "CL", "lat": -23.50, "lng": -68.20, "mineral": "Lithium", "operator": "SQM/Albemarle", "detail": "World's largest lithium brine; ~30% global supply"},
    {"name": "Greenbushes", "country": "AU", "lat": -33.85, "lng": 116.07, "mineral": "Lithium", "operator": "Talison (Tianqi/Albemarle)", "detail": "Largest hard-rock lithium mine"},
    {"name": "Salar de Uyuni", "country": "BO", "lat": -20.13, "lng": -67.49, "mineral": "Lithium", "operator": "YLB", "detail": "Largest lithium reserve untapped (~21Mt)"},
    {"name": "Salar del Hombre Muerto", "country": "AR", "lat": -25.40, "lng": -67.07, "mineral": "Lithium", "operator": "Livent/Allkem", "detail": "Argentina lithium triangle"},
    # Rare Earths
    {"name": "Bayan Obo", "country": "CN", "lat": 41.77, "lng": 109.95, "mineral": "Rare Earths", "operator": "China Northern", "detail": "World's largest REE mine; 70%+ global supply"},
    {"name": "Mountain Pass", "country": "US", "lat": 35.48, "lng": -115.53, "mineral": "Rare Earths", "operator": "MP Materials", "detail": "Only US REE mine; DoD strategic"},
    {"name": "Mount Weld", "country": "AU", "lat": -28.86, "lng": 122.55, "mineral": "Rare Earths", "operator": "Lynas Rare Earths", "detail": "Largest non-Chinese REE producer"},
    {"name": "Nechalacho", "country": "CA", "lat": 62.65, "lng": -112.34, "mineral": "Rare Earths", "operator": "Vital Metals", "detail": "Canadian heavy REE source"},
    # Cobalt / Copper
    {"name": "Mutanda Mine", "country": "CD", "lat": -10.83, "lng": 25.72, "mineral": "Cobalt/Copper", "operator": "Glencore", "detail": "DRC copper belt; 20% global cobalt"},
    {"name": "Tenke Fungurume", "country": "CD", "lat": -10.61, "lng": 26.18, "mineral": "Cobalt/Copper", "operator": "CMOC (China)", "detail": "Chinese-controlled; 2nd largest cobalt"},
    {"name": "Kamoa-Kakula", "country": "CD", "lat": -10.75, "lng": 25.27, "mineral": "Copper", "operator": "Ivanhoe Mines", "detail": "Highest-grade major copper mine"},
    {"name": "Escondida", "country": "CL", "lat": -24.27, "lng": -69.07, "mineral": "Copper", "operator": "BHP", "detail": "World's largest copper mine"},
    {"name": "Grasberg", "country": "ID", "lat": -4.06, "lng": 137.12, "mineral": "Copper/Gold", "operator": "Freeport-McMoRan", "detail": "World's 2nd largest copper; largest gold"},
    # Nickel
    {"name": "Sudbury Basin", "country": "CA", "lat": 46.50, "lng": -81.00, "mineral": "Nickel", "operator": "Vale/Glencore", "detail": "Canada nickel/PGM hub"},
    {"name": "Norilsk", "country": "RU", "lat": 69.35, "lng": 88.20, "mineral": "Nickel/Palladium", "operator": "Nornickel", "detail": "Largest palladium producer; 11% world nickel"},
    {"name": "Sorowako", "country": "ID", "lat": -2.53, "lng": 121.36, "mineral": "Nickel", "operator": "Vale/PT Vale", "detail": "Indonesia nickel boom; EV battery supply"},
    # Gold/PGM
    {"name": "Witwatersrand", "country": "ZA", "lat": -26.20, "lng": 27.50, "mineral": "Gold/Platinum", "operator": "Various", "detail": "Historic 40% world gold; deep mines"},
    {"name": "Carlin Trend", "country": "US", "lat": 40.83, "lng": -116.10, "mineral": "Gold", "operator": "Newmont/Barrick", "detail": "Largest US gold producing district"},
    # Uranium
    {"name": "Cigar Lake", "country": "CA", "lat": 58.05, "lng": -104.48, "mineral": "Uranium", "operator": "Cameco", "detail": "World's highest-grade uranium"},
    {"name": "Olympic Dam", "country": "AU", "lat": -30.44, "lng": 136.88, "mineral": "Uranium/Copper", "operator": "BHP", "detail": "Largest known uranium deposit"},
    # Iron
    {"name": "Carajás Mine", "country": "BR", "lat": -6.07, "lng": -50.16, "mineral": "Iron Ore", "operator": "Vale", "detail": "World's largest iron ore mine"},
    {"name": "Pilbara Iron", "country": "AU", "lat": -22.60, "lng": 117.80, "mineral": "Iron Ore", "operator": "BHP/Rio Tinto/FMG", "detail": "Pilbara region; 60% seaborne iron trade"},
    # Tin
    {"name": "Bangka-Belitung", "country": "ID", "lat": -2.74, "lng": 106.45, "mineral": "Tin", "operator": "PT Timah", "detail": "Largest tin producer; offshore dredging"},
    # Niobium
    {"name": "Araxá", "country": "BR", "lat": -19.59, "lng": -46.95, "mineral": "Niobium", "operator": "CBMM", "detail": "85% of world niobium; steel additive"},
    # Graphite
    {"name": "Balama", "country": "MZ", "lat": -13.34, "lng": 38.55, "mineral": "Graphite", "operator": "Syrah Resources", "detail": "Largest graphite mine outside China"},
]


# ═══════════════ DISEASE OUTBREAKS ═══════════════
# Tracked from WHO/CDC/ECDC reports — significant ongoing/recent outbreaks
DISEASE_OUTBREAKS = [
    {"name": "Mpox (Clade Ib)", "country": "CD", "lat": -4.04, "lng": 21.76, "disease": "Mpox", "severity": "HIGH",
     "cases": "30,000+", "detail": "WHO PHEIC 2024; Clade Ib spreading via close contact; Central/East Africa"},
    {"name": "Marburg Outbreak", "country": "RW", "lat": -1.95, "lng": 30.06, "disease": "Marburg virus", "severity": "EXTREME",
     "cases": "60+", "detail": "Rwanda 2024; healthcare workers affected; 30% CFR"},
    {"name": "H5N1 in dairy cattle", "country": "US", "lat": 40.00, "lng": -100.00, "disease": "H5N1 Avian Flu", "severity": "MEDIUM",
     "cases": "Cattle/poultry + 50+ humans", "detail": "Bovine spread unprecedented; California/Colorado"},
    {"name": "Cholera Sudan", "country": "SD", "lat": 15.50, "lng": 32.56, "disease": "Cholera", "severity": "HIGH",
     "cases": "20,000+", "detail": "Civil war collapse of WASH; flooding"},
    {"name": "Dengue Brazil", "country": "BR", "lat": -15.78, "lng": -47.93, "disease": "Dengue", "severity": "HIGH",
     "cases": "10M+ in 2024", "detail": "Worst outbreak in history; climate-linked"},
    {"name": "Ebola Uganda", "country": "UG", "lat": 0.32, "lng": 32.58, "disease": "Ebola", "severity": "HIGH",
     "cases": "Sporadic", "detail": "Sudan ebolavirus; Mubende district historic outbreak"},
    {"name": "Polio Pakistan", "country": "PK", "lat": 33.70, "lng": 73.06, "disease": "Wild Poliovirus", "severity": "HIGH",
     "cases": "60+", "detail": "Pakistan/Afghanistan only WPV1 endemic; vaccine refusal"},
    {"name": "Measles Europe", "country": "RO", "lat": 44.43, "lng": 26.10, "disease": "Measles", "severity": "MEDIUM",
     "cases": "30,000+ EU/EEA", "detail": "Vaccine hesitancy; Romania, UK, France worst hit"},
    {"name": "MERS-CoV", "country": "SA", "lat": 24.71, "lng": 46.67, "disease": "MERS Coronavirus", "severity": "MEDIUM",
     "cases": "Sporadic", "detail": "Camel reservoir; healthcare-associated clusters"},
    {"name": "Nipah Kerala", "country": "IN", "lat": 11.41, "lng": 75.69, "disease": "Nipah virus", "severity": "HIGH",
     "cases": "Cluster", "detail": "Bat-borne; ~70% CFR; Kerala recurrent outbreaks"},
    {"name": "Lassa Fever", "country": "NG", "lat": 9.08, "lng": 8.68, "disease": "Lassa", "severity": "MEDIUM",
     "cases": "1000+", "detail": "Endemic West Africa; rodent-borne"},
    {"name": "Yellow Fever Colombia", "country": "CO", "lat": 4.71, "lng": -74.07, "disease": "Yellow fever", "severity": "MEDIUM",
     "cases": "Sporadic", "detail": "Tolima/Putumayo regions; vaccine campaigns"},
    {"name": "Crimean-Congo HF", "country": "TR", "lat": 39.93, "lng": 32.86, "disease": "CCHF", "severity": "MEDIUM",
     "cases": "Hundreds", "detail": "Tick-borne; agricultural workers; Anatolia"},
    {"name": "Zika Resurgence", "country": "TH", "lat": 13.75, "lng": 100.50, "disease": "Zika virus", "severity": "MEDIUM",
     "cases": "Hundreds", "detail": "Aedes mosquito; Bangkok/Phuket"},
    {"name": "Diphtheria Yemen", "country": "YE", "lat": 15.37, "lng": 44.19, "disease": "Diphtheria", "severity": "HIGH",
     "cases": "Thousands", "detail": "Civil war collapse of immunization"},
]


# ═══════════════ PROTESTS / CIVIL UNREST ═══════════════
# Major ongoing or recent significant protest movements
PROTEST_EVENTS = [
    {"name": "Georgia EU Protests", "country": "GE", "lat": 41.72, "lng": 44.79, "size": "100k+", "duration": "ongoing",
     "detail": "Pro-EU demonstrations against gov suspending EU accession; Tbilisi"},
    {"name": "Romania Election Crisis", "country": "RO", "lat": 44.43, "lng": 26.10, "size": "10k+", "duration": "2024-2025",
     "detail": "Election annulment; Călin Georgescu controversy"},
    {"name": "South Korea Martial Law Aftermath", "country": "KR", "lat": 37.57, "lng": 126.98, "size": "1M+", "duration": "ongoing",
     "detail": "Yoon impeachment protests; National Assembly"},
    {"name": "Bangladesh Quota Protests", "country": "BD", "lat": 23.81, "lng": 90.41, "size": "Mass", "duration": "2024",
     "detail": "Student-led; toppled Sheikh Hasina; interim Yunus government"},
    {"name": "Kenya Finance Bill Protests", "country": "KE", "lat": -1.29, "lng": 36.82, "size": "Mass", "duration": "ongoing",
     "detail": "Gen-Z led; tax hikes; parliament stormed"},
    {"name": "Mozambique Election Unrest", "country": "MZ", "lat": -25.97, "lng": 32.58, "size": "Wide", "duration": "ongoing",
     "detail": "Frelimo win disputed; deaths in clashes"},
    {"name": "Argentina Anti-Austerity", "country": "AR", "lat": -34.61, "lng": -58.40, "size": "Recurring", "duration": "ongoing",
     "detail": "Milei reform protests; pension/labor"},
    {"name": "France Pension Aftermath", "country": "FR", "lat": 48.86, "lng": 2.35, "size": "Recurring", "duration": "ongoing",
     "detail": "Macron unpopular; budget crisis"},
    {"name": "Iran Hijab Protests", "country": "IR", "lat": 35.69, "lng": 51.39, "size": "Underground", "duration": "ongoing",
     "detail": "Mahsa Amini legacy; women defying laws"},
    {"name": "Hong Kong Continued", "country": "HK", "lat": 22.32, "lng": 114.17, "size": "Suppressed", "duration": "ongoing",
     "detail": "National Security Law dissent; Article 23"},
    {"name": "Israel Hostage Protests", "country": "IL", "lat": 32.07, "lng": 34.79, "size": "100k+", "duration": "ongoing",
     "detail": "Tel Aviv weekly; demanding hostage deal"},
    {"name": "Serbia EU Protests", "country": "RS", "lat": 44.79, "lng": 20.45, "size": "10k+", "duration": "ongoing",
     "detail": "Novi Sad station collapse; anti-Vučić"},
]


# ═══════════════ INTERNET OUTAGES / GPS JAMMING ═══════════════
# Known persistent internet disruption / GPS jamming hotspots
INTERNET_OUTAGES = [
    {"name": "Kaliningrad GPS Jamming", "country": "RU", "lat": 54.71, "lng": 20.51, "type": "gps_jam", "severity": "HIGH",
     "detail": "Russian electronic warfare; affects Baltic aviation/maritime"},
    {"name": "Eastern Mediterranean GPS", "country": "Multi", "lat": 33.50, "lng": 34.50, "type": "gps_jam", "severity": "HIGH",
     "detail": "Israeli/Hezbollah EW; civil aviation impacts"},
    {"name": "Black Sea GPS", "country": "Multi", "lat": 44.00, "lng": 35.00, "type": "gps_jam", "severity": "HIGH",
     "detail": "Russian jamming during Ukraine war"},
    {"name": "Iran Internet Throttling", "country": "IR", "lat": 35.69, "lng": 51.39, "type": "throttle", "severity": "EXTREME",
     "detail": "Persistent during protests; full blackouts during unrest"},
    {"name": "Myanmar Internet Restrictions", "country": "MM", "lat": 19.75, "lng": 96.10, "type": "blackout", "severity": "EXTREME",
     "detail": "Junta-imposed; rotating regional blackouts since 2021"},
    {"name": "Sudan Internet Collapse", "country": "SD", "lat": 15.50, "lng": 32.56, "type": "blackout", "severity": "EXTREME",
     "detail": "Civil war infrastructure damage; nationwide outages"},
    {"name": "Pakistan Social Media Ban", "country": "PK", "lat": 33.70, "lng": 73.06, "type": "block", "severity": "HIGH",
     "detail": "X/Twitter persistent block since 2024"},
    {"name": "Russia VPN Crackdown", "country": "RU", "lat": 55.75, "lng": 37.62, "type": "block", "severity": "HIGH",
     "detail": "VPN protocols increasingly blocked; YouTube degraded"},
    {"name": "Cuba Internet Restrictions", "country": "CU", "lat": 23.13, "lng": -82.38, "type": "throttle", "severity": "HIGH",
     "detail": "ETECSA monopoly; routine throttling during dissent"},
    {"name": "North Korea Air Gap", "country": "KP", "lat": 39.02, "lng": 125.75, "type": "blackout", "severity": "EXTREME",
     "detail": "No public internet; Kwangmyong intranet only"},
    {"name": "Ethiopia Tigray", "country": "ET", "lat": 13.50, "lng": 39.47, "type": "blackout", "severity": "HIGH",
     "detail": "Periodic shutdowns since 2020; Amhara region similar"},
    {"name": "Afghanistan Restrictions", "country": "AF", "lat": 34.53, "lng": 69.17, "type": "block", "severity": "HIGH",
     "detail": "Taliban controls; Facebook/select apps blocked"},
]


# ═══════════════ CYBER THREAT ADVISORIES ═══════════════
CYBER_ADVISORIES = [
    {"id": "CISA-AA26-094A", "severity": "CRITICAL", "title": "Volt Typhoon — China-state living-off-the-land",
     "vendor": "Multi (US infra)", "country": "US", "lat": 38.90, "lng": -77.04,
     "actor": "China APT (Volt Typhoon)", "vector": "Compromised SOHO routers + LOTL",
     "detail": "Targeting US critical infrastructure — water, power, comms — for pre-positioning"},
    {"id": "CISA-AA26-088B", "severity": "CRITICAL", "title": "Ivanti Connect Secure — RCE chain",
     "vendor": "Ivanti", "country": "US", "lat": 39.04, "lng": -77.49,
     "actor": "China nation-state", "vector": "CVE-2026-21887 + CVE-2024-46805",
     "detail": "Auth bypass + command injection; mass exploitation; thousands of devices compromised"},
    {"id": "CVE-2026-3094", "severity": "CRITICAL", "title": "XZ Utils backdoor recurrence",
     "vendor": "OSS supply chain", "country": "Multi", "lat": 50.11, "lng": 8.68,
     "actor": "Suspected nation-state", "vector": "Upstream tarball injection",
     "detail": "Hidden SSH auth bypass in libxz; affects Debian/Fedora unstable; sshd RCE"},
    {"id": "CISA-AA26-072C", "severity": "HIGH", "title": "MOVEit Transfer — new auth bypass",
     "vendor": "Progress", "country": "US", "lat": 42.36, "lng": -71.06,
     "actor": "Cl0p ransomware", "vector": "SQL injection + privesc",
     "detail": "Mass data theft from managed file transfer servers; 200+ orgs hit"},
    {"id": "CERT-EU-26-018", "severity": "HIGH", "title": "Russian APT28 phishing — EU government",
     "vendor": "Microsoft 365", "country": "BE", "lat": 50.85, "lng": 4.35,
     "actor": "APT28 (Fancy Bear, GRU)", "vector": "Spear-phishing + token theft",
     "detail": "Targeting EU foreign ministries and Ukraine support orgs"},
    {"id": "CISA-AA26-058D", "severity": "HIGH", "title": "Fortinet FortiOS — pre-auth RCE",
     "vendor": "Fortinet", "country": "US", "lat": 37.42, "lng": -122.08,
     "actor": "Multiple", "vector": "CVE-2026-21762 heap overflow",
     "detail": "Pre-auth RCE on SSL VPN; actively exploited; CISA emergency directive"},
    {"id": "NCSC-UK-26-009", "severity": "HIGH", "title": "Iranian APT34 espionage — Gulf telecoms",
     "vendor": "Cisco/Juniper", "country": "AE", "lat": 25.27, "lng": 55.30,
     "actor": "APT34 (OilRig, MOIS)", "vector": "Supply chain + watering hole",
     "detail": "Long-term access to Gulf telecom carriers for SIGINT"},
    {"id": "CVE-2026-1234", "severity": "CRITICAL", "title": "Microsoft Outlook NTLM relay",
     "vendor": "Microsoft", "country": "US", "lat": 47.64, "lng": -122.13,
     "actor": "APT29 (Cozy Bear, SVR)", "vector": "Specially crafted email triggers NTLM auth",
     "detail": "0-click; preview pane is enough; credential theft + lateral movement"},
    {"id": "CISA-AA26-035E", "severity": "MEDIUM", "title": "BlackCat (ALPHV) ransomware revival",
     "vendor": "Healthcare sector", "country": "US", "lat": 41.88, "lng": -87.63,
     "actor": "BlackCat affiliates", "vector": "Initial access broker + double extortion",
     "detail": "Hospital networks; HHS warning; $2M+ avg ransom demand"},
    {"id": "CERT-FR-26-022", "severity": "HIGH", "title": "Lazarus Group cryptocurrency theft",
     "vendor": "Crypto exchanges", "country": "KP", "lat": 39.02, "lng": 125.75,
     "actor": "Lazarus Group (DPRK)", "vector": "Social engineering + malicious npm packages",
     "detail": "$680M stolen YTD 2026; funds DPRK weapons program"},
    {"id": "CVE-2026-2156", "severity": "HIGH", "title": "VMware vCenter heap overflow",
     "vendor": "VMware/Broadcom", "country": "Multi", "lat": 37.42, "lng": -122.08,
     "actor": "Multiple", "vector": "DCERPC heap overflow",
     "detail": "Pre-auth RCE on vCenter Server; widespread enterprise impact"},
    {"id": "CERT-AU-26-005", "severity": "MEDIUM", "title": "Optus follow-up — Medibank-style breach",
     "vendor": "Telco/Healthcare", "country": "AU", "lat": -33.87, "lng": 151.21,
     "actor": "Unknown ransomware", "vector": "API enumeration",
     "detail": "10M customer records exfiltrated; sensitive health data leaked"},
    {"id": "CISA-AA26-019F", "severity": "HIGH", "title": "Cisco IOS XE web UI",
     "vendor": "Cisco", "country": "US", "lat": 37.42, "lng": -122.08,
     "actor": "Unknown", "vector": "CVE-2026-20198 implant chain",
     "detail": "Privilege escalation via web management; tens of thousands compromised"},
    {"id": "BSI-26-014", "severity": "HIGH", "title": "German energy sector phishing wave",
     "vendor": "SCADA/ICS", "country": "DE", "lat": 52.52, "lng": 13.41,
     "actor": "Sandworm (GRU)", "vector": "Spear-phishing + ICS-targeting malware",
     "detail": "Targeting Energiewende infrastructure; BSI HIGH alert"},
    {"id": "JPCERT-26-008", "severity": "MEDIUM", "title": "Japanese semiconductor supply chain",
     "vendor": "Multi (chip equipment)", "country": "JP", "lat": 35.68, "lng": 139.69,
     "actor": "BlackTech (China)", "vector": "Router firmware backdoors",
     "detail": "Long-term implants in branch routers; semi-equipment IP theft"},
]


# ═══════════════ GPS JAMMING ZONES ═══════════════
GPS_JAMMING_ZONES = [
    {"name": "Kaliningrad EW Hub", "country": "RU", "lat": 54.71, "lng": 20.51, "radius_km": 180,
     "actor": "Russian Western MD", "intensity": "EXTREME",
     "detail": "Krasukha-4 + Murmansk-BN; affects Baltic aviation/maritime; Finnair routinely diverts"},
    {"name": "Eastern Med GPS spoofing", "country": "Multi", "lat": 33.50, "lng": 34.50, "radius_km": 280,
     "actor": "IDF + Hezbollah EW", "intensity": "EXTREME",
     "detail": "Ben Gurion arrivals affected; ships report GPS phantom positions"},
    {"name": "Black Sea NW", "country": "RU/UA", "lat": 45.20, "lng": 33.00, "radius_km": 320,
     "actor": "Russian Black Sea Fleet", "intensity": "EXTREME",
     "detail": "Crimea-based jamming; affects civil aviation Romania/Bulgaria; ship AIS spoofing"},
    {"name": "Persian Gulf", "country": "IR/Multi", "lat": 26.50, "lng": 53.00, "radius_km": 250,
     "actor": "IRGC EW", "intensity": "HIGH",
     "detail": "Tanker GPS spoofing near Hormuz; vessels falsely reported in Iranian waters"},
    {"name": "Korean DMZ", "country": "KP", "lat": 38.32, "lng": 127.30, "radius_km": 90,
     "actor": "DPRK", "intensity": "HIGH",
     "detail": "Periodic large-scale jamming; KCNA confirmed exercises 2024-25"},
    {"name": "Murmansk / Kola", "country": "RU", "lat": 68.97, "lng": 33.08, "radius_km": 220,
     "actor": "Russian Northern Fleet", "intensity": "HIGH",
     "detail": "Arctic NATO exercises trigger jamming; Norwegian airspace affected"},
    {"name": "Sahel insurgency belt", "country": "Multi", "lat": 14.50, "lng": 4.00, "radius_km": 350,
     "actor": "JNIM/ISGS + Russian PMC", "intensity": "MEDIUM",
     "detail": "Localized jamming around military convoys; UAV countermeasures"},
    {"name": "Syrian airspace", "country": "SY", "lat": 35.00, "lng": 38.50, "radius_km": 280,
     "actor": "Russian Khmeimim", "intensity": "HIGH",
     "detail": "Krasukha-2/4 deployed; affects civil aviation E. Med"},
    {"name": "Crimea peninsula", "country": "UA/RU", "lat": 45.00, "lng": 34.00, "radius_km": 200,
     "actor": "Russian forces", "intensity": "EXTREME",
     "detail": "Continuous EW; navigation denied for Ukrainian drones and missiles"},
    {"name": "Baltic Sea center", "country": "Multi", "lat": 57.00, "lng": 19.50, "radius_km": 200,
     "actor": "Russian Baltic Fleet", "intensity": "MEDIUM",
     "detail": "Episodes during Russian naval exercises; AIS spoofing"},
]


# ═══════════════ DISPLACEMENT / REFUGEE FLOWS ═══════════════
DISPLACEMENT_FLOWS = [
    {"name": "Syria → Türkiye", "from_country": "SY", "to_country": "TR",
     "from_lat": 36.20, "from_lng": 37.16, "to_lat": 37.06, "to_lng": 37.38,
     "population": 3200000, "year_started": 2011, "status": "ongoing",
     "detail": "Largest single refugee population in any country"},
    {"name": "Ukraine → Poland", "from_country": "UA", "to_country": "PL",
     "from_lat": 50.45, "from_lng": 30.52, "to_lat": 52.23, "to_lng": 21.01,
     "population": 1600000, "year_started": 2022, "status": "ongoing",
     "detail": "Post-Feb 2022 invasion; mostly women and children"},
    {"name": "Ukraine → Germany", "from_country": "UA", "to_country": "DE",
     "from_lat": 49.84, "from_lng": 24.03, "to_lat": 52.52, "to_lng": 13.41,
     "population": 1100000, "year_started": 2022, "status": "ongoing",
     "detail": "Second-largest UA destination in EU"},
    {"name": "Sudan → Chad", "from_country": "SD", "to_country": "TD",
     "from_lat": 13.45, "from_lng": 22.45, "to_lat": 12.13, "to_lng": 15.05,
     "population": 720000, "year_started": 2023, "status": "ongoing",
     "detail": "Darfur conflict + RSF/SAF civil war"},
    {"name": "Myanmar → Bangladesh (Rohingya)", "from_country": "MM", "to_country": "BD",
     "from_lat": 20.85, "from_lng": 92.36, "to_lat": 21.20, "to_lng": 92.16,
     "population": 960000, "year_started": 2017, "status": "ongoing",
     "detail": "Cox's Bazar camps; world's largest refugee settlement"},
    {"name": "Venezuela → Colombia", "from_country": "VE", "to_country": "CO",
     "from_lat": 10.50, "from_lng": -66.93, "to_lat": 4.71, "to_lng": -74.07,
     "population": 2900000, "year_started": 2015, "status": "ongoing",
     "detail": "Largest displacement crisis in the Western Hemisphere"},
    {"name": "Afghanistan → Pakistan", "from_country": "AF", "to_country": "PK",
     "from_lat": 34.52, "from_lng": 69.18, "to_lat": 33.69, "to_lng": 73.05,
     "population": 1700000, "year_started": 2021, "status": "ongoing",
     "detail": "Post-Taliban takeover; PK now deporting many"},
    {"name": "Afghanistan → Iran", "from_country": "AF", "to_country": "IR",
     "from_lat": 34.52, "from_lng": 69.18, "to_lat": 35.69, "to_lng": 51.39,
     "population": 3800000, "year_started": 2021, "status": "ongoing",
     "detail": "Iran hosts more Afghans than any other country"},
    {"name": "South Sudan → Uganda", "from_country": "SS", "to_country": "UG",
     "from_lat": 4.85, "from_lng": 31.58, "to_lat": 0.32, "to_lng": 32.58,
     "population": 940000, "year_started": 2013, "status": "ongoing",
     "detail": "Bidi Bidi camp — largest in Africa"},
    {"name": "DRC → Uganda", "from_country": "CD", "to_country": "UG",
     "from_lat": -1.68, "from_lng": 29.22, "to_lat": 0.32, "to_lng": 32.58,
     "population": 510000, "year_started": 2017, "status": "ongoing",
     "detail": "Ituri/North Kivu armed groups; M23 resurgence"},
    {"name": "Gaza internal displacement", "from_country": "PS", "to_country": "PS",
     "from_lat": 31.50, "from_lng": 34.47, "to_lat": 31.34, "to_lng": 34.30,
     "population": 1900000, "year_started": 2023, "status": "active",
     "detail": "85% of Gaza population displaced; multiple displacement events"},
    {"name": "Somalia → Kenya", "from_country": "SO", "to_country": "KE",
     "from_lat": 2.05, "from_lng": 45.32, "to_lat": -0.06, "to_lng": 40.32,
     "population": 280000, "year_started": 1991, "status": "ongoing",
     "detail": "Dadaab camp complex; multi-decade crisis"},
    {"name": "Eritrea → Ethiopia", "from_country": "ER", "to_country": "ET",
     "from_lat": 15.32, "from_lng": 38.93, "to_lat": 13.50, "to_lng": 39.47,
     "population": 150000, "year_started": 2000, "status": "ongoing",
     "detail": "National conscription escapees; Tigray war complications"},
    {"name": "Haiti → Dominican Republic", "from_country": "HT", "to_country": "DO",
     "from_lat": 18.59, "from_lng": -72.30, "to_lat": 18.74, "to_lng": -70.16,
     "population": 500000, "year_started": 2010, "status": "ongoing",
     "detail": "Gang collapse + earthquake aftermath; DR mass deportations"},
]


# ═══════════════ AIR QUALITY (PM2.5 readings, major cities) ═══════════════
AIR_QUALITY = [
    {"name": "New Delhi", "country": "IN", "lat": 28.61, "lng": 77.21, "pm25": 178, "aqi_label": "Hazardous",
     "detail": "Crop burning + diesel + dust; perennial winter crisis"},
    {"name": "Lahore", "country": "PK", "lat": 31.55, "lng": 74.34, "pm25": 195, "aqi_label": "Hazardous",
     "detail": "Worst-ranked global city most days; smog seasons drive shutdowns"},
    {"name": "Dhaka", "country": "BD", "lat": 23.81, "lng": 90.41, "pm25": 142, "aqi_label": "Hazardous",
     "detail": "Brick kilns + traffic; pre-monsoon peak"},
    {"name": "Beijing", "country": "CN", "lat": 39.91, "lng": 116.39, "pm25": 58, "aqi_label": "Unhealthy",
     "detail": "Improved from 2013 peaks but still 10x WHO guideline"},
    {"name": "Jakarta", "country": "ID", "lat": -6.21, "lng": 106.85, "pm25": 76, "aqi_label": "Unhealthy",
     "detail": "Coal plants + traffic; legal action against gov 2023"},
    {"name": "Mumbai", "country": "IN", "lat": 19.08, "lng": 72.88, "pm25": 95, "aqi_label": "Very Unhealthy", "detail": ""},
    {"name": "Kolkata", "country": "IN", "lat": 22.57, "lng": 88.36, "pm25": 105, "aqi_label": "Very Unhealthy", "detail": ""},
    {"name": "Karachi", "country": "PK", "lat": 24.86, "lng": 67.01, "pm25": 88, "aqi_label": "Very Unhealthy", "detail": ""},
    {"name": "Cairo", "country": "EG", "lat": 30.04, "lng": 31.24, "pm25": 65, "aqi_label": "Unhealthy", "detail": ""},
    {"name": "Tehran", "country": "IR", "lat": 35.69, "lng": 51.39, "pm25": 72, "aqi_label": "Unhealthy", "detail": ""},
    {"name": "Mexico City", "country": "MX", "lat": 19.43, "lng": -99.13, "pm25": 32, "aqi_label": "USG", "detail": ""},
    {"name": "Santiago", "country": "CL", "lat": -33.45, "lng": -70.67, "pm25": 38, "aqi_label": "USG",
     "detail": "Winter inversions trap pollution in valley"},
    {"name": "Bangkok", "country": "TH", "lat": 13.75, "lng": 100.50, "pm25": 48, "aqi_label": "Unhealthy", "detail": ""},
    {"name": "Hanoi", "country": "VN", "lat": 21.03, "lng": 105.85, "pm25": 56, "aqi_label": "Unhealthy", "detail": ""},
    {"name": "Ulaanbaatar", "country": "MN", "lat": 47.92, "lng": 106.92, "pm25": 168, "aqi_label": "Hazardous",
     "detail": "Coal-burning yurt heating; -40°C winters"},
    {"name": "Los Angeles", "country": "US", "lat": 34.05, "lng": -118.24, "pm25": 14, "aqi_label": "Moderate", "detail": ""},
    {"name": "New York", "country": "US", "lat": 40.71, "lng": -74.01, "pm25": 11, "aqi_label": "Moderate", "detail": ""},
    {"name": "London", "country": "GB", "lat": 51.51, "lng": -0.13, "pm25": 13, "aqi_label": "Moderate", "detail": ""},
    {"name": "Paris", "country": "FR", "lat": 48.86, "lng": 2.35, "pm25": 15, "aqi_label": "Moderate", "detail": ""},
    {"name": "Berlin", "country": "DE", "lat": 52.52, "lng": 13.41, "pm25": 12, "aqi_label": "Moderate", "detail": ""},
    {"name": "Tokyo", "country": "JP", "lat": 35.68, "lng": 139.69, "pm25": 10, "aqi_label": "Moderate", "detail": ""},
    {"name": "Seoul", "country": "KR", "lat": 37.57, "lng": 126.98, "pm25": 24, "aqi_label": "Moderate",
     "detail": "Includes contribution from Chinese transboundary haze"},
    {"name": "Sydney", "country": "AU", "lat": -33.87, "lng": 151.21, "pm25": 8, "aqi_label": "Good",
     "detail": "Bushfire seasons spike to Hazardous"},
    {"name": "Reykjavík", "country": "IS", "lat": 64.13, "lng": -21.82, "pm25": 4, "aqi_label": "Good", "detail": ""},
    {"name": "Helsinki", "country": "FI", "lat": 60.17, "lng": 24.94, "pm25": 5, "aqi_label": "Good", "detail": ""},
    {"name": "Oslo", "country": "NO", "lat": 59.91, "lng": 10.75, "pm25": 6, "aqi_label": "Good", "detail": ""},
    {"name": "São Paulo", "country": "BR", "lat": -23.55, "lng": -46.63, "pm25": 22, "aqi_label": "Moderate", "detail": ""},
    {"name": "Buenos Aires", "country": "AR", "lat": -34.60, "lng": -58.38, "pm25": 16, "aqi_label": "Moderate", "detail": ""},
    {"name": "Lagos", "country": "NG", "lat": 6.45, "lng": 3.40, "pm25": 68, "aqi_label": "Unhealthy", "detail": ""},
    {"name": "Johannesburg", "country": "ZA", "lat": -26.20, "lng": 28.05, "pm25": 35, "aqi_label": "USG", "detail": ""},
    {"name": "Istanbul", "country": "TR", "lat": 41.00, "lng": 28.98, "pm25": 30, "aqi_label": "Moderate", "detail": ""},
    {"name": "Moscow", "country": "RU", "lat": 55.76, "lng": 37.62, "pm25": 18, "aqi_label": "Moderate", "detail": ""},
    {"name": "Madrid", "country": "ES", "lat": 40.42, "lng": -3.70, "pm25": 11, "aqi_label": "Moderate", "detail": ""},
    {"name": "Dubai", "country": "AE", "lat": 25.27, "lng": 55.30, "pm25": 42, "aqi_label": "Unhealthy",
     "detail": "Sandstorms drive episodic spikes"},
    {"name": "Singapore", "country": "SG", "lat": 1.35, "lng": 103.82, "pm25": 18, "aqi_label": "Moderate",
     "detail": "Indonesian haze events spike to Hazardous"},
]


# ═══════════════ SEA ICE (polar climate snapshot) ═══════════════
ARCTIC_SEA_ICE = {
    "name": "Arctic Sea Ice (March 2026 max)",
    "extent_million_km2": 14.1,
    "anomaly_pct": -7.8,
    "edge_lat": 70.5,
    "detail": "Below 1981-2010 median; thinning multiyear ice; Arctic Amplification accelerating",
}

ANTARCTIC_SEA_ICE = {
    "name": "Antarctic Sea Ice (Sep 2025 max)",
    "extent_million_km2": 17.0,
    "anomaly_pct": -11.2,
    "edge_lat": -66.0,
    "detail": "Record-low extent for 4th year running; previously stable, now in regime shift",
}


# ═══════════════ SUPPLY CHAIN DISRUPTIONS ═══════════════
SUPPLY_CHAIN_DISRUPTIONS = [
    {"name": "Houthi Red Sea attacks", "lat": 13.50, "lng": 43.10, "severity": "EXTREME",
     "type": "armed_conflict", "country": "YE",
     "detail": "Container traffic via Suez down 65% YoY; major lines diverting via Cape of Good Hope (+10-14 days, +20% fuel)"},
    {"name": "Panama Canal drought", "lat": 9.10, "lng": -79.70, "severity": "HIGH",
     "type": "climate", "country": "PA",
     "detail": "Gatun Lake levels low; transits limited; queue surcharges; partially relieved 2025"},
    {"name": "Strait of Hormuz tensions", "lat": 26.60, "lng": 56.30, "severity": "HIGH",
     "type": "geopolitical", "country": "IR",
     "detail": "IRGC vessel boardings; insurance war-risk premiums elevated"},
    {"name": "Black Sea grain corridor", "lat": 44.60, "lng": 33.60, "severity": "HIGH",
     "type": "armed_conflict", "country": "UA",
     "detail": "Russian missile/drone strikes on Odesa port infrastructure; grain exports volatile"},
    {"name": "Bab-el-Mandeb mining", "lat": 12.60, "lng": 43.50, "severity": "HIGH",
     "type": "armed_conflict", "country": "YE",
     "detail": "Houthi naval mines + UUVs; dive teams cleared multiple"},
    {"name": "Taiwan Strait military exercises", "lat": 24.50, "lng": 119.50, "severity": "MEDIUM",
     "type": "geopolitical", "country": "TW",
     "detail": "PLA blockade drills disrupt container schedules; semi supply chain risk"},
    {"name": "TSMC Taiwan earthquake risk", "lat": 24.77, "lng": 121.01, "severity": "MEDIUM",
     "type": "natural", "country": "TW",
     "detail": "April 2024 quake briefly halted fab output; concentration risk for advanced chips"},
    {"name": "Suez tanker grounding (recurring)", "lat": 30.40, "lng": 32.35, "severity": "MEDIUM",
     "type": "navigation", "country": "EG",
     "detail": "Wind-driven grounding events; Ever Given precedent; multi-day delays"},
    {"name": "Baltimore Key Bridge collapse", "lat": 39.22, "lng": -76.53, "severity": "HIGH",
     "type": "infrastructure", "country": "US",
     "detail": "March 2024 ship strike; port cleared but rebuild ongoing; auto/coal exports affected"},
    {"name": "Chinese rare-earth export curbs", "lat": 41.13, "lng": 109.84, "severity": "HIGH",
     "type": "trade_policy", "country": "CN",
     "detail": "Bayan Obo region; gallium/germanium/graphite controls; downstream chip impacts"},
    {"name": "DRC cobalt production swings", "lat": -10.72, "lng": 25.47, "severity": "MEDIUM",
     "type": "resource", "country": "CD",
     "detail": "Glencore Kamoto + Mutanda; 70% global cobalt; M23 conflict edges in"},
    {"name": "Chile copper drought", "lat": -22.46, "lng": -68.92, "severity": "MEDIUM",
     "type": "climate", "country": "CL",
     "detail": "Atacama mines water-rationed; Codelco production revised down"},
    {"name": "Philippine semiconductor flooding", "lat": 14.60, "lng": 121.00, "severity": "MEDIUM",
     "type": "natural", "country": "PH",
     "detail": "Typhoon-driven backend assembly disruption; auto chips affected"},
    {"name": "Mexico northbound rail backlog", "lat": 28.45, "lng": -106.42, "severity": "MEDIUM",
     "type": "logistics", "country": "MX",
     "detail": "USBP closures cause cross-border rail backups; auto JIT disruption"},
]


# ═══════════════ ACTIVE VOLCANOES ═══════════════
ACTIVE_VOLCANOES = [
    {"name": "Kilauea", "country": "US", "lat": 19.421, "lng": -155.287, "elev_m": 1222, "vtype": "shield", "status": "ERUPTING", "detail": "Halemaʻumaʻu summit lava lake activity"},
    {"name": "Mauna Loa", "country": "US", "lat": 19.475, "lng": -155.608, "elev_m": 4170, "vtype": "shield", "status": "UNREST", "detail": "World's largest volcano; elevated seismicity"},
    {"name": "Mount St. Helens", "country": "US", "lat": 46.200, "lng": -122.188, "elev_m": 2549, "vtype": "stratovolcano", "status": "MONITOR", "detail": "Cascade Range; 1980 eruption"},
    {"name": "Yellowstone Caldera", "country": "US", "lat": 44.428, "lng": -110.588, "elev_m": 2805, "vtype": "caldera", "status": "MONITOR", "detail": "Supervolcano; hydrothermal swarms tracked weekly"},
    {"name": "Mount Rainier", "country": "US", "lat": 46.852, "lng": -121.760, "elev_m": 4392, "vtype": "stratovolcano", "status": "MONITOR", "detail": "Lahar threat to Seattle metro"},
    {"name": "Popocatépetl", "country": "MX", "lat": 19.023, "lng": -98.628, "elev_m": 5426, "vtype": "stratovolcano", "status": "ERUPTING", "detail": "Persistent ash plumes; CDMX/Puebla exclusion"},
    {"name": "Colima", "country": "MX", "lat": 19.514, "lng": -103.620, "elev_m": 3850, "vtype": "stratovolcano", "status": "UNREST", "detail": "Frequent vulcanian explosions"},
    {"name": "Fuego", "country": "GT", "lat": 14.473, "lng": -90.880, "elev_m": 3763, "vtype": "stratovolcano", "status": "ERUPTING", "detail": "Daily strombolian; 2018 deadly PDC"},
    {"name": "Pacaya", "country": "GT", "lat": 14.381, "lng": -90.601, "elev_m": 2552, "vtype": "complex", "status": "UNREST", "detail": "Tourist volcano near Guatemala City"},
    {"name": "Santiaguito", "country": "GT", "lat": 14.739, "lng": -91.568, "elev_m": 2500, "vtype": "lava dome", "status": "ERUPTING", "detail": "Active lava dome complex"},
    {"name": "Arenal", "country": "CR", "lat": 10.463, "lng": -84.703, "elev_m": 1670, "vtype": "stratovolcano", "status": "MONITOR", "detail": "Resting since 2010"},
    {"name": "Poás", "country": "CR", "lat": 10.200, "lng": -84.233, "elev_m": 2708, "vtype": "stratovolcano", "status": "UNREST", "detail": "Acid crater lake; phreatic events"},
    {"name": "Nevado del Ruiz", "country": "CO", "lat": 4.892, "lng": -75.324, "elev_m": 5321, "vtype": "stratovolcano", "status": "UNREST", "detail": "1985 Armero lahar killed 23,000"},
    {"name": "Galeras", "country": "CO", "lat": 1.220, "lng": -77.359, "elev_m": 4276, "vtype": "stratovolcano", "status": "UNREST", "detail": "Pasto city under threat"},
    {"name": "Sangay", "country": "EC", "lat": -2.005, "lng": -78.341, "elev_m": 5286, "vtype": "stratovolcano", "status": "ERUPTING", "detail": "Continuous activity since 1934"},
    {"name": "Cotopaxi", "country": "EC", "lat": -0.683, "lng": -78.437, "elev_m": 5897, "vtype": "stratovolcano", "status": "UNREST", "detail": "Glacier-clad; lahar risk to Quito"},
    {"name": "Reventador", "country": "EC", "lat": -0.078, "lng": -77.656, "elev_m": 3562, "vtype": "stratovolcano", "status": "ERUPTING", "detail": "Persistent vulcanian explosions"},
    {"name": "Sabancaya", "country": "PE", "lat": -15.787, "lng": -71.857, "elev_m": 5967, "vtype": "stratovolcano", "status": "ERUPTING", "detail": "Ash plumes affect Arequipa"},
    {"name": "Ubinas", "country": "PE", "lat": -16.355, "lng": -70.903, "elev_m": 5672, "vtype": "stratovolcano", "status": "UNREST", "detail": "Peru's most active volcano"},
    {"name": "Villarrica", "country": "CL", "lat": -39.420, "lng": -71.930, "elev_m": 2847, "vtype": "stratovolcano", "status": "UNREST", "detail": "Lava lake; tourist hub Pucón"},
    {"name": "Etna", "country": "IT", "lat": 37.751, "lng": 14.994, "elev_m": 3357, "vtype": "stratovolcano", "status": "ERUPTING", "detail": "Europe's most active; Catania airport closures"},
    {"name": "Stromboli", "country": "IT", "lat": 38.789, "lng": 15.213, "elev_m": 924, "vtype": "stratovolcano", "status": "ERUPTING", "detail": "Continuous activity for >2000 years"},
    {"name": "Vesuvius", "country": "IT", "lat": 40.821, "lng": 14.426, "elev_m": 1281, "vtype": "stratovolcano", "status": "MONITOR", "detail": "3M people in red zone; 79 AD Pompeii"},
    {"name": "Campi Flegrei", "country": "IT", "lat": 40.827, "lng": 14.139, "elev_m": 458, "vtype": "caldera", "status": "UNREST", "detail": "Bradyseism; 500K residents over caldera"},
    {"name": "Santorini", "country": "GR", "lat": 36.404, "lng": 25.396, "elev_m": 367, "vtype": "caldera", "status": "UNREST", "detail": "Earthquake swarm 2025"},
    {"name": "Mount Cameroon", "country": "CM", "lat": 4.203, "lng": 9.170, "elev_m": 4040, "vtype": "stratovolcano", "status": "MONITOR", "detail": "West Africa's most active"},
    {"name": "Nyiragongo", "country": "CD", "lat": -1.520, "lng": 29.250, "elev_m": 3470, "vtype": "stratovolcano", "status": "UNREST", "detail": "World's largest lava lake; threatens Goma"},
    {"name": "Erta Ale", "country": "ET", "lat": 13.601, "lng": 40.671, "elev_m": 613, "vtype": "shield", "status": "ERUPTING", "detail": "Persistent lava lake in Danakil"},
    {"name": "Ol Doinyo Lengai", "country": "TZ", "lat": -2.764, "lng": 35.914, "elev_m": 2962, "vtype": "stratovolcano", "status": "UNREST", "detail": "Only natrocarbonatite volcano on Earth"},
    {"name": "Ambrym", "country": "VU", "lat": -16.250, "lng": 168.120, "elev_m": 1334, "vtype": "caldera", "status": "ERUPTING", "detail": "Twin lava lakes typically active"},
    {"name": "Yasur", "country": "VU", "lat": -19.532, "lng": 169.447, "elev_m": 361, "vtype": "stratovolcano", "status": "ERUPTING", "detail": "Continuous strombolian for centuries"},
    {"name": "Krakatau (Anak)", "country": "ID", "lat": -6.102, "lng": 105.423, "elev_m": 813, "vtype": "caldera", "status": "ERUPTING", "detail": "2018 collapse → tsunami; cone rebuilding"},
    {"name": "Merapi", "country": "ID", "lat": -7.540, "lng": 110.446, "elev_m": 2910, "vtype": "stratovolcano", "status": "ERUPTING", "detail": "Java's deadliest; Yogyakarta"},
    {"name": "Sinabung", "country": "ID", "lat": 3.170, "lng": 98.392, "elev_m": 2460, "vtype": "stratovolcano", "status": "UNREST", "detail": "Reawakened 2010"},
    {"name": "Marapi", "country": "ID", "lat": -0.381, "lng": 100.473, "elev_m": 2891, "vtype": "complex", "status": "ERUPTING", "detail": "Sumatra; 2023 deadly eruption"},
    {"name": "Semeru", "country": "ID", "lat": -8.108, "lng": 112.922, "elev_m": 3676, "vtype": "stratovolcano", "status": "ERUPTING", "detail": "East Java; daily explosions"},
    {"name": "Lewotobi", "country": "ID", "lat": -8.530, "lng": 122.775, "elev_m": 1703, "vtype": "stratovolcano", "status": "ERUPTING", "detail": "Flores; 2024 deadly paroxysm"},
    {"name": "Taal", "country": "PH", "lat": 14.002, "lng": 120.993, "elev_m": 311, "vtype": "caldera", "status": "UNREST", "detail": "2020 eruption affected Manila"},
    {"name": "Mayon", "country": "PH", "lat": 13.257, "lng": 123.685, "elev_m": 2462, "vtype": "stratovolcano", "status": "UNREST", "detail": "Symmetric cone; lahar/PDC threat"},
    {"name": "Pinatubo", "country": "PH", "lat": 15.143, "lng": 120.350, "elev_m": 1486, "vtype": "stratovolcano", "status": "MONITOR", "detail": "1991 second-largest 20th C eruption"},
    {"name": "Kanlaon", "country": "PH", "lat": 10.412, "lng": 123.132, "elev_m": 2435, "vtype": "stratovolcano", "status": "ERUPTING", "detail": "Negros island; 2024 eruption"},
    {"name": "Sakurajima", "country": "JP", "lat": 31.585, "lng": 130.657, "elev_m": 1117, "vtype": "stratovolcano", "status": "ERUPTING", "detail": "600K Kagoshima residents nearby"},
    {"name": "Aso", "country": "JP", "lat": 32.884, "lng": 131.104, "elev_m": 1592, "vtype": "caldera", "status": "UNREST", "detail": "World's largest active caldera"},
    {"name": "Suwanosejima", "country": "JP", "lat": 29.638, "lng": 129.714, "elev_m": 796, "vtype": "stratovolcano", "status": "ERUPTING", "detail": "Persistent strombolian"},
    {"name": "Shiveluch", "country": "RU", "lat": 56.653, "lng": 161.360, "elev_m": 3283, "vtype": "stratovolcano", "status": "ERUPTING", "detail": "Kamchatka; major 2023 eruption"},
    {"name": "Klyuchevskoy", "country": "RU", "lat": 56.056, "lng": 160.642, "elev_m": 4754, "vtype": "stratovolcano", "status": "ERUPTING", "detail": "Eurasia's tallest active volcano"},
    {"name": "Bezymianny", "country": "RU", "lat": 55.972, "lng": 160.595, "elev_m": 2882, "vtype": "stratovolcano", "status": "ERUPTING", "detail": "Kamchatka; major paroxysms"},
    {"name": "Grímsvötn", "country": "IS", "lat": 64.416, "lng": -17.316, "elev_m": 1725, "vtype": "subglacial", "status": "MONITOR", "detail": "Iceland's most frequent; jokulhlaup risk"},
    {"name": "Reykjanes Peninsula", "country": "IS", "lat": 63.870, "lng": -22.270, "elev_m": 200, "vtype": "fissure", "status": "ERUPTING", "detail": "Sundhnúkur fissure; Grindavík evacuated"},
]


# ═══════════════ AI DATA CENTERS / GPU CLUSTERS ═══════════════
AI_DATA_CENTERS = [
    {"name": "OpenAI/Microsoft Mt. Pleasant", "operator": "Microsoft", "country": "US", "lat": 42.73, "lng": -87.92, "chip": "H100/B200", "chip_count": 100000, "power_mw": 300, "status": "ACTIVE"},
    {"name": "xAI Memphis Colossus", "operator": "xAI", "country": "US", "lat": 35.13, "lng": -90.05, "chip": "H100", "chip_count": 200000, "power_mw": 400, "status": "ACTIVE"},
    {"name": "Meta Mesa", "operator": "Meta", "country": "US", "lat": 33.39, "lng": -111.61, "chip": "H100", "chip_count": 60000, "power_mw": 200, "status": "ACTIVE"},
    {"name": "Meta Eagle Mountain", "operator": "Meta", "country": "US", "lat": 40.31, "lng": -112.04, "chip": "H100", "chip_count": 50000, "power_mw": 180, "status": "ACTIVE"},
    {"name": "Google Council Bluffs", "operator": "Google", "country": "US", "lat": 41.26, "lng": -95.84, "chip": "TPU v5p", "chip_count": 100000, "power_mw": 250, "status": "ACTIVE"},
    {"name": "Google Pryor Creek", "operator": "Google", "country": "US", "lat": 36.31, "lng": -95.31, "chip": "TPU v5", "chip_count": 80000, "power_mw": 200, "status": "ACTIVE"},
    {"name": "AWS Ashburn", "operator": "AWS", "country": "US", "lat": 39.04, "lng": -77.49, "chip": "Trainium/H100", "chip_count": 150000, "power_mw": 400, "status": "ACTIVE"},
    {"name": "Stargate Abilene", "operator": "Microsoft/OpenAI", "country": "US", "lat": 32.45, "lng": -99.74, "chip": "B200", "chip_count": 400000, "power_mw": 1200, "status": "BUILDING"},
    {"name": "AWS Seattle HQ", "operator": "AWS", "country": "US", "lat": 47.62, "lng": -122.34, "chip": "Trainium2", "chip_count": 60000, "power_mw": 150, "status": "ACTIVE"},
    {"name": "Oracle Salt Lake", "operator": "Oracle", "country": "US", "lat": 40.76, "lng": -111.89, "chip": "H100", "chip_count": 30000, "power_mw": 100, "status": "ACTIVE"},
    {"name": "CoreWeave Plano", "operator": "CoreWeave", "country": "US", "lat": 33.02, "lng": -96.70, "chip": "H100/H200", "chip_count": 45000, "power_mw": 120, "status": "ACTIVE"},
    {"name": "Crusoe Abilene", "operator": "Crusoe", "country": "US", "lat": 32.45, "lng": -99.74, "chip": "H100", "chip_count": 100000, "power_mw": 200, "status": "BUILDING"},
    {"name": "Lambda San Francisco", "operator": "Lambda", "country": "US", "lat": 37.77, "lng": -122.42, "chip": "H100", "chip_count": 20000, "power_mw": 60, "status": "ACTIVE"},
    {"name": "Microsoft Quincy", "operator": "Microsoft", "country": "US", "lat": 47.23, "lng": -119.85, "chip": "H100", "chip_count": 80000, "power_mw": 250, "status": "ACTIVE"},
    {"name": "Microsoft San Antonio", "operator": "Microsoft", "country": "US", "lat": 29.42, "lng": -98.49, "chip": "H100", "chip_count": 70000, "power_mw": 220, "status": "ACTIVE"},
    {"name": "Google Mons", "operator": "Google", "country": "BE", "lat": 50.45, "lng": 3.95, "chip": "TPU v5", "chip_count": 40000, "power_mw": 120, "status": "ACTIVE"},
    {"name": "Microsoft Dublin", "operator": "Microsoft", "country": "IE", "lat": 53.35, "lng": -6.26, "chip": "H100", "chip_count": 30000, "power_mw": 100, "status": "ACTIVE"},
    {"name": "AWS Frankfurt", "operator": "AWS", "country": "DE", "lat": 50.11, "lng": 8.68, "chip": "H100/Trainium", "chip_count": 50000, "power_mw": 150, "status": "ACTIVE"},
    {"name": "Mistral Paris", "operator": "Mistral", "country": "FR", "lat": 48.86, "lng": 2.35, "chip": "H100", "chip_count": 8000, "power_mw": 25, "status": "ACTIVE"},
    {"name": "G42 Abu Dhabi", "operator": "G42", "country": "AE", "lat": 24.47, "lng": 54.37, "chip": "H100/B200", "chip_count": 50000, "power_mw": 150, "status": "ACTIVE"},
    {"name": "Saudi HUMAIN", "operator": "PIF/HUMAIN", "country": "SA", "lat": 24.71, "lng": 46.68, "chip": "B200", "chip_count": 100000, "power_mw": 300, "status": "BUILDING"},
    {"name": "Equinix Singapore", "operator": "Equinix", "country": "SG", "lat": 1.35, "lng": 103.82, "chip": "H100", "chip_count": 25000, "power_mw": 80, "status": "ACTIVE"},
    {"name": "Alibaba Hangzhou", "operator": "Alibaba", "country": "CN", "lat": 30.27, "lng": 120.15, "chip": "H800/Ascend", "chip_count": 100000, "power_mw": 250, "status": "ACTIVE"},
    {"name": "ByteDance AI Cluster", "operator": "ByteDance", "country": "CN", "lat": 39.92, "lng": 116.39, "chip": "H800", "chip_count": 150000, "power_mw": 350, "status": "ACTIVE"},
    {"name": "Huawei Atlas Shenzhen", "operator": "Huawei", "country": "CN", "lat": 22.54, "lng": 114.06, "chip": "Ascend 910B", "chip_count": 80000, "power_mw": 200, "status": "ACTIVE"},
    {"name": "Tencent Tianjin", "operator": "Tencent", "country": "CN", "lat": 39.13, "lng": 117.20, "chip": "H800", "chip_count": 60000, "power_mw": 180, "status": "ACTIVE"},
    {"name": "Baidu Beijing", "operator": "Baidu", "country": "CN", "lat": 40.05, "lng": 116.30, "chip": "Kunlun/Ascend", "chip_count": 40000, "power_mw": 130, "status": "ACTIVE"},
    {"name": "Yandex Vladimir", "operator": "Yandex", "country": "RU", "lat": 56.13, "lng": 40.41, "chip": "H100/A100", "chip_count": 15000, "power_mw": 50, "status": "ACTIVE"},
    {"name": "Sber Moscow", "operator": "Sber", "country": "RU", "lat": 55.76, "lng": 37.62, "chip": "A100", "chip_count": 10000, "power_mw": 35, "status": "ACTIVE"},
    {"name": "NAVER Sejong", "operator": "NAVER", "country": "KR", "lat": 36.48, "lng": 127.29, "chip": "H100", "chip_count": 30000, "power_mw": 100, "status": "ACTIVE"},
    {"name": "SoftBank Hokkaido", "operator": "SoftBank", "country": "JP", "lat": 42.78, "lng": 141.69, "chip": "B200", "chip_count": 50000, "power_mw": 150, "status": "BUILDING"},
    {"name": "Reliance Jamnagar AI", "operator": "Reliance", "country": "IN", "lat": 22.47, "lng": 70.07, "chip": "B200", "chip_count": 100000, "power_mw": 300, "status": "BUILDING"},
    {"name": "Tata Hyderabad", "operator": "Tata", "country": "IN", "lat": 17.39, "lng": 78.49, "chip": "H100", "chip_count": 25000, "power_mw": 80, "status": "ACTIVE"},
    {"name": "Yotta Maharashtra", "operator": "Yotta", "country": "IN", "lat": 19.04, "lng": 73.07, "chip": "H100", "chip_count": 16000, "power_mw": 50, "status": "ACTIVE"},
    {"name": "Stargate UAE", "operator": "OpenAI/G42", "country": "AE", "lat": 24.47, "lng": 54.37, "chip": "B200", "chip_count": 200000, "power_mw": 600, "status": "PLANNED"},
]


# ═══════════════ TECH HEADQUARTERS ═══════════════
TECH_HQS = [
    {"name": "Apple", "country": "US", "lat": 37.3349, "lng": -122.0090, "category": "Hardware/Services", "mcap": 3500},
    {"name": "Microsoft", "country": "US", "lat": 47.6396, "lng": -122.1283, "category": "Software/Cloud", "mcap": 3300},
    {"name": "NVIDIA", "country": "US", "lat": 37.3704, "lng": -121.9636, "category": "AI Chips", "mcap": 3200},
    {"name": "Alphabet (Google)", "country": "US", "lat": 37.4220, "lng": -122.0841, "category": "Search/Cloud", "mcap": 2200},
    {"name": "Amazon", "country": "US", "lat": 47.6225, "lng": -122.3361, "category": "E-commerce/Cloud", "mcap": 2100},
    {"name": "Meta", "country": "US", "lat": 37.4848, "lng": -122.1484, "category": "Social/AR", "mcap": 1500},
    {"name": "TSMC", "country": "TW", "lat": 24.7741, "lng": 121.0167, "category": "Semiconductors", "mcap": 950},
    {"name": "Tesla", "country": "US", "lat": 30.2226, "lng": -97.6197, "category": "EV/AI", "mcap": 1100},
    {"name": "Broadcom", "country": "US", "lat": 37.3500, "lng": -122.0000, "category": "Chips/Software", "mcap": 850},
    {"name": "Oracle", "country": "US", "lat": 37.5293, "lng": -122.2645, "category": "Database/Cloud", "mcap": 480},
    {"name": "Samsung", "country": "KR", "lat": 37.2580, "lng": 127.0610, "category": "Hardware/Memory", "mcap": 380},
    {"name": "ASML", "country": "NL", "lat": 51.4111, "lng": 5.4536, "category": "EUV Lithography", "mcap": 300},
    {"name": "Tencent", "country": "CN", "lat": 22.5410, "lng": 113.9340, "category": "Social/Gaming", "mcap": 480},
    {"name": "Alibaba", "country": "CN", "lat": 30.1830, "lng": 120.0680, "category": "E-commerce/Cloud", "mcap": 220},
    {"name": "Salesforce", "country": "US", "lat": 37.7898, "lng": -122.3942, "category": "CRM/AI", "mcap": 290},
    {"name": "AMD", "country": "US", "lat": 37.4030, "lng": -121.9806, "category": "CPUs/GPUs", "mcap": 250},
    {"name": "Adobe", "country": "US", "lat": 37.3318, "lng": -121.8917, "category": "Creative SaaS", "mcap": 220},
    {"name": "SAP", "country": "DE", "lat": 49.2944, "lng": 8.6433, "category": "Enterprise SW", "mcap": 240},
    {"name": "Intel", "country": "US", "lat": 37.3879, "lng": -121.9636, "category": "CPUs/Foundry", "mcap": 130},
    {"name": "Cisco", "country": "US", "lat": 37.4106, "lng": -121.9528, "category": "Networking", "mcap": 200},
    {"name": "IBM", "country": "US", "lat": 41.1090, "lng": -73.7220, "category": "Enterprise/Quantum", "mcap": 200},
    {"name": "Sony", "country": "JP", "lat": 35.6313, "lng": 139.7423, "category": "Entertainment/Sensors", "mcap": 110},
    {"name": "Netflix", "country": "US", "lat": 37.2562, "lng": -121.9651, "category": "Streaming", "mcap": 290},
    {"name": "ServiceNow", "country": "US", "lat": 37.4031, "lng": -121.9810, "category": "Workflow SaaS", "mcap": 200},
    {"name": "Palantir", "country": "US", "lat": 38.8800, "lng": -104.7700, "category": "Analytics/Defense", "mcap": 180},
    {"name": "Shopify", "country": "CA", "lat": 45.4172, "lng": -75.7011, "category": "E-commerce", "mcap": 130},
    {"name": "Spotify", "country": "SE", "lat": 59.3293, "lng": 18.0686, "category": "Audio Streaming", "mcap": 90},
    {"name": "Booking.com", "country": "NL", "lat": 52.3676, "lng": 4.9041, "category": "Travel", "mcap": 130},
    {"name": "Sea Limited (Shopee)", "country": "SG", "lat": 1.2966, "lng": 103.7764, "category": "SE Asia tech", "mcap": 60},
    {"name": "MercadoLibre", "country": "AR", "lat": -34.6037, "lng": -58.3816, "category": "LatAm e-com", "mcap": 80},
]


# ═══════════════ STARTUP / TECH HUBS ═══════════════
STARTUP_HUBS = [
    {"name": "Silicon Valley", "country": "US", "lat": 37.39, "lng": -122.08, "rank": 1, "unicorns": 360},
    {"name": "New York", "country": "US", "lat": 40.71, "lng": -74.01, "rank": 2, "unicorns": 130},
    {"name": "London", "country": "GB", "lat": 51.51, "lng": -0.13, "rank": 3, "unicorns": 75},
    {"name": "Boston", "country": "US", "lat": 42.36, "lng": -71.06, "rank": 4, "unicorns": 35},
    {"name": "Beijing", "country": "CN", "lat": 39.91, "lng": 116.39, "rank": 5, "unicorns": 90},
    {"name": "Shanghai", "country": "CN", "lat": 31.23, "lng": 121.47, "rank": 6, "unicorns": 50},
    {"name": "Los Angeles", "country": "US", "lat": 34.05, "lng": -118.24, "rank": 7, "unicorns": 50},
    {"name": "Tel Aviv", "country": "IL", "lat": 32.08, "lng": 34.78, "rank": 8, "unicorns": 30},
    {"name": "Bangalore", "country": "IN", "lat": 12.97, "lng": 77.59, "rank": 9, "unicorns": 40},
    {"name": "Seoul", "country": "KR", "lat": 37.57, "lng": 126.98, "rank": 10, "unicorns": 25},
    {"name": "Berlin", "country": "DE", "lat": 52.52, "lng": 13.41, "rank": 11, "unicorns": 28},
    {"name": "Singapore", "country": "SG", "lat": 1.35, "lng": 103.82, "rank": 12, "unicorns": 30},
    {"name": "Paris", "country": "FR", "lat": 48.86, "lng": 2.35, "rank": 13, "unicorns": 32},
    {"name": "Stockholm", "country": "SE", "lat": 59.33, "lng": 18.07, "rank": 14, "unicorns": 15},
    {"name": "Tokyo", "country": "JP", "lat": 35.68, "lng": 139.69, "rank": 15, "unicorns": 18},
    {"name": "Toronto", "country": "CA", "lat": 43.65, "lng": -79.38, "rank": 16, "unicorns": 22},
    {"name": "Amsterdam", "country": "NL", "lat": 52.37, "lng": 4.90, "rank": 17, "unicorns": 18},
    {"name": "Sydney", "country": "AU", "lat": -33.87, "lng": 151.21, "rank": 18, "unicorns": 12},
    {"name": "Dubai", "country": "AE", "lat": 25.20, "lng": 55.27, "rank": 19, "unicorns": 8},
    {"name": "São Paulo", "country": "BR", "lat": -23.55, "lng": -46.63, "rank": 20, "unicorns": 18},
    {"name": "Mexico City", "country": "MX", "lat": 19.43, "lng": -99.13, "rank": 21, "unicorns": 8},
    {"name": "Austin", "country": "US", "lat": 30.27, "lng": -97.74, "rank": 22, "unicorns": 25},
    {"name": "Miami", "country": "US", "lat": 25.76, "lng": -80.19, "rank": 23, "unicorns": 12},
    {"name": "Hong Kong", "country": "HK", "lat": 22.32, "lng": 114.17, "rank": 24, "unicorns": 18},
]


# ═══════════════ FINANCIAL CENTERS ═══════════════
FINANCIAL_CENTERS = [
    {"name": "New York", "country": "US", "lat": 40.71, "lng": -74.01, "ftype": "primary", "aum": 36.5},
    {"name": "London", "country": "GB", "lat": 51.51, "lng": -0.13, "ftype": "primary", "aum": 15.2},
    {"name": "Hong Kong", "country": "HK", "lat": 22.32, "lng": 114.17, "ftype": "primary", "aum": 12.0},
    {"name": "Singapore", "country": "SG", "lat": 1.35, "lng": 103.82, "ftype": "primary", "aum": 5.4},
    {"name": "Tokyo", "country": "JP", "lat": 35.68, "lng": 139.69, "ftype": "primary", "aum": 7.5},
    {"name": "Shanghai", "country": "CN", "lat": 31.23, "lng": 121.47, "ftype": "primary", "aum": 7.0},
    {"name": "Frankfurt", "country": "DE", "lat": 50.11, "lng": 8.68, "ftype": "primary", "aum": 2.6},
    {"name": "Zurich", "country": "CH", "lat": 47.37, "lng": 8.55, "ftype": "primary", "aum": 4.0},
    {"name": "Geneva", "country": "CH", "lat": 46.20, "lng": 6.15, "ftype": "primary", "aum": 3.5},
    {"name": "Dubai (DIFC)", "country": "AE", "lat": 25.21, "lng": 55.28, "ftype": "primary", "aum": 0.4},
    {"name": "Luxembourg", "country": "LU", "lat": 49.61, "lng": 6.13, "ftype": "offshore", "aum": 5.5},
    {"name": "Cayman Islands", "country": "KY", "lat": 19.31, "lng": -81.25, "ftype": "offshore", "aum": 5.0},
    {"name": "Jersey", "country": "JE", "lat": 49.21, "lng": -2.13, "ftype": "offshore", "aum": 0.5},
    {"name": "Guernsey", "country": "GG", "lat": 49.45, "lng": -2.59, "ftype": "offshore", "aum": 0.4},
    {"name": "Isle of Man", "country": "IM", "lat": 54.24, "lng": -4.55, "ftype": "offshore", "aum": 0.1},
    {"name": "Bermuda", "country": "BM", "lat": 32.31, "lng": -64.78, "ftype": "offshore", "aum": 0.4},
    {"name": "BVI", "country": "VG", "lat": 18.42, "lng": -64.62, "ftype": "offshore", "aum": 1.5},
]


# ═══════════════ COMMODITY HUBS ═══════════════
COMMODITY_HUBS = [
    {"name": "Rotterdam", "country": "NL", "lat": 51.92, "lng": 4.48, "commodity": "Oil/Container", "detail": "Europe's largest port; oil refining hub"},
    {"name": "Singapore (Bunkering)", "country": "SG", "lat": 1.35, "lng": 103.82, "commodity": "Oil trading", "detail": "Asia oil pricing benchmark"},
    {"name": "Houston", "country": "US", "lat": 29.76, "lng": -95.37, "commodity": "Oil/LNG", "detail": "US oil capital"},
    {"name": "Cushing", "country": "US", "lat": 35.98, "lng": -96.77, "commodity": "WTI delivery", "detail": "WTI futures delivery point"},
    {"name": "Chicago (CBOT)", "country": "US", "lat": 41.88, "lng": -87.63, "commodity": "Grains", "detail": "CBOT corn/wheat/soy"},
    {"name": "Geneva (Trading)", "country": "CH", "lat": 46.20, "lng": 6.15, "commodity": "Trading HQs", "detail": "Glencore/Vitol/Trafigura HQs"},
    {"name": "London (LME)", "country": "GB", "lat": 51.51, "lng": -0.09, "commodity": "Base metals", "detail": "London Metal Exchange"},
    {"name": "Shanghai (SHFE)", "country": "CN", "lat": 31.23, "lng": 121.47, "commodity": "Metals/Energy", "detail": "Shanghai Futures Exchange"},
    {"name": "Dalian (DCE)", "country": "CN", "lat": 38.91, "lng": 121.61, "commodity": "Iron ore/Soy", "detail": "DCE iron ore benchmark"},
    {"name": "Antwerp", "country": "BE", "lat": 51.22, "lng": 4.40, "commodity": "Chemicals/Diamonds", "detail": "World's diamond capital"},
    {"name": "Fujairah", "country": "AE", "lat": 25.13, "lng": 56.33, "commodity": "Bunker/Oil", "detail": "Middle East bunkering hub"},
]


# ═══════════════ TRADE ROUTES ═══════════════
TRADE_ROUTES = [
    {"name": "Asia-Europe Suez", "rtype": "sea", "tonnage_pa": 1200,
     "waypoints": [[121.47, 31.23], [103.82, 1.35], [56.25, 26.57], [32.55, 30.05], [13.41, 35.85], [-5.35, 36.13], [-0.13, 51.51]]},
    {"name": "Trans-Pacific (CN-US)", "rtype": "sea", "tonnage_pa": 950,
     "waypoints": [[121.47, 31.23], [139.69, 35.68], [-157.86, 21.31], [-118.24, 33.74]]},
    {"name": "Trans-Atlantic", "rtype": "sea", "tonnage_pa": 600,
     "waypoints": [[-74.01, 40.71], [-9.14, 38.72], [-0.13, 51.51], [4.48, 51.92]]},
    {"name": "Russia-China Power of Siberia", "rtype": "pipeline", "tonnage_pa": 60,
     "waypoints": [[78.36, 65.50], [101.59, 52.27], [116.39, 39.91]]},
    {"name": "BRI Land Bridge (CN-EU Rail)", "rtype": "rail", "tonnage_pa": 18,
     "waypoints": [[114.06, 22.54], [85.99, 41.83], [76.95, 43.26], [76.94, 49.81], [37.62, 55.76], [16.37, 48.21], [13.41, 52.52]]},
    {"name": "INSTC (India-Iran-Russia)", "rtype": "multi", "tonnage_pa": 30,
     "waypoints": [[72.88, 19.08], [60.59, 25.30], [51.39, 35.69], [49.65, 40.40], [37.62, 55.76]]},
    {"name": "Strait of Hormuz Tankers", "rtype": "sea", "tonnage_pa": 850,
     "waypoints": [[50.10, 26.40], [56.25, 26.57], [60.59, 25.30], [72.88, 19.08]]},
    {"name": "Bab el-Mandeb Suez Approach", "rtype": "sea", "tonnage_pa": 480,
     "waypoints": [[103.82, 1.35], [78.16, 8.78], [43.32, 12.58], [32.55, 30.05]]},
    {"name": "Cape of Good Hope (Suez Diversion)", "rtype": "sea", "tonnage_pa": 280,
     "waypoints": [[103.82, 1.35], [78.16, 8.78], [55.45, -4.62], [18.42, -33.92], [-9.14, 38.72]]},
    {"name": "Northern Sea Route", "rtype": "sea", "tonnage_pa": 35,
     "waypoints": [[101.59, 52.27], [69.30, 73.50], [50.05, 70.97], [33.08, 68.97], [4.48, 51.92]]},
    {"name": "Panama Canal", "rtype": "sea", "tonnage_pa": 520,
     "waypoints": [[121.47, 31.23], [139.69, 35.68], [-79.50, 9.08], [-74.01, 40.71]]},
    {"name": "Mexico-US Land", "rtype": "land", "tonnage_pa": 720,
     "waypoints": [[-99.13, 19.43], [-100.32, 25.69], [-97.50, 30.27], [-95.37, 29.76], [-87.63, 41.88]]},
]


# ═══════════════ INTEL HOTSPOTS ═══════════════
INTEL_HOTSPOTS = [
    {"name": "Langley (CIA)", "country": "US", "lat": 38.95, "lng": -77.15, "category": "intel_hq"},
    {"name": "Fort Meade (NSA)", "country": "US", "lat": 39.11, "lng": -76.77, "category": "intel_hq"},
    {"name": "Vauxhall Cross (MI6)", "country": "GB", "lat": 51.49, "lng": -0.12, "category": "intel_hq"},
    {"name": "Cheltenham (GCHQ)", "country": "GB", "lat": 51.90, "lng": -2.12, "category": "intel_hq"},
    {"name": "Yasenevo (SVR)", "country": "RU", "lat": 55.61, "lng": 37.55, "category": "intel_hq"},
    {"name": "Lubyanka (FSB)", "country": "RU", "lat": 55.76, "lng": 37.63, "category": "intel_hq"},
    {"name": "Pullach (BND)", "country": "DE", "lat": 48.05, "lng": 11.52, "category": "intel_hq"},
    {"name": "DGSE Paris", "country": "FR", "lat": 48.88, "lng": 2.40, "category": "intel_hq"},
    {"name": "Glilot (Mossad)", "country": "IL", "lat": 32.13, "lng": 34.81, "category": "intel_hq"},
    {"name": "Tel Aviv (Unit 8200)", "country": "IL", "lat": 32.08, "lng": 34.78, "category": "intel_hq"},
    {"name": "MSS Beijing", "country": "CN", "lat": 39.92, "lng": 116.39, "category": "intel_hq"},
    {"name": "MOIS Tehran", "country": "IR", "lat": 35.69, "lng": 51.39, "category": "intel_hq"},
    {"name": "ISI Islamabad", "country": "PK", "lat": 33.69, "lng": 73.04, "category": "intel_hq"},
    {"name": "RAW New Delhi", "country": "IN", "lat": 28.61, "lng": 77.21, "category": "intel_hq"},
    {"name": "Pyongyang (RGB)", "country": "KP", "lat": 39.02, "lng": 125.75, "category": "intel_hq"},
    {"name": "Vienna UN/IAEA", "country": "AT", "lat": 48.23, "lng": 16.41, "category": "diplomatic"},
    {"name": "Geneva UN", "country": "CH", "lat": 46.22, "lng": 6.14, "category": "diplomatic"},
    {"name": "Brussels (NATO HQ)", "country": "BE", "lat": 50.88, "lng": 4.42, "category": "military_hq"},
    {"name": "SHAPE Mons", "country": "BE", "lat": 50.46, "lng": 3.94, "category": "military_hq"},
    {"name": "EUCOM Stuttgart", "country": "DE", "lat": 48.78, "lng": 9.18, "category": "military_hq"},
    {"name": "AFRICOM Stuttgart", "country": "DE", "lat": 48.78, "lng": 9.19, "category": "military_hq"},
    {"name": "CENTCOM Tampa", "country": "US", "lat": 27.85, "lng": -82.50, "category": "military_hq"},
    {"name": "INDOPACOM Honolulu", "country": "US", "lat": 21.34, "lng": -157.94, "category": "military_hq"},
    {"name": "USCYBERCOM Fort Meade", "country": "US", "lat": 39.11, "lng": -76.78, "category": "military_hq"},
    {"name": "Pentagon", "country": "US", "lat": 38.87, "lng": -77.06, "category": "military_hq"},
    {"name": "Kremlin", "country": "RU", "lat": 55.75, "lng": 37.62, "category": "political_hq"},
    {"name": "Zhongnanhai", "country": "CN", "lat": 39.92, "lng": 116.38, "category": "political_hq"},
    {"name": "Camp David", "country": "US", "lat": 39.65, "lng": -77.47, "category": "political_hq"},
    {"name": "Davos nexus", "country": "CH", "lat": 46.80, "lng": 9.83, "category": "diplomatic"},
    {"name": "Qatar diplomatic hub", "country": "QA", "lat": 25.29, "lng": 51.53, "category": "diplomatic"},
]


# ═══════════════ SANCTIONS PRESSURE ═══════════════
SANCTIONS_PRESSURE = [
    {"country": "Russia", "iso": "RU", "lat": 61.52, "lng": 105.32, "score": 95, "programs": ["US OFAC", "EU", "UK", "G7", "AU"]},
    {"country": "Iran", "iso": "IR", "lat": 32.43, "lng": 53.69, "score": 92, "programs": ["US OFAC", "EU", "UN", "UK"]},
    {"country": "North Korea", "iso": "KP", "lat": 40.34, "lng": 127.51, "score": 98, "programs": ["UN", "US OFAC", "EU", "JP", "KR"]},
    {"country": "Belarus", "iso": "BY", "lat": 53.71, "lng": 27.95, "score": 85, "programs": ["US OFAC", "EU", "UK"]},
    {"country": "Syria", "iso": "SY", "lat": 34.80, "lng": 38.99, "score": 90, "programs": ["US OFAC", "EU", "AL"]},
    {"country": "Venezuela", "iso": "VE", "lat": 6.42, "lng": -66.59, "score": 80, "programs": ["US OFAC", "EU", "CA"]},
    {"country": "Cuba", "iso": "CU", "lat": 21.52, "lng": -77.78, "score": 75, "programs": ["US OFAC"]},
    {"country": "Myanmar", "iso": "MM", "lat": 21.91, "lng": 95.95, "score": 70, "programs": ["US OFAC", "EU", "UK"]},
    {"country": "Sudan", "iso": "SD", "lat": 12.86, "lng": 30.22, "score": 65, "programs": ["US OFAC", "EU"]},
    {"country": "Afghanistan", "iso": "AF", "lat": 33.93, "lng": 67.71, "score": 65, "programs": ["UN", "US OFAC"]},
    {"country": "South Sudan", "iso": "SS", "lat": 6.88, "lng": 31.31, "score": 55, "programs": ["UN", "US OFAC", "EU"]},
    {"country": "Somalia", "iso": "SO", "lat": 5.15, "lng": 46.20, "score": 50, "programs": ["UN", "US OFAC"]},
    {"country": "Mali", "iso": "ML", "lat": 17.57, "lng": -3.99, "score": 50, "programs": ["UN", "EU"]},
    {"country": "Nicaragua", "iso": "NI", "lat": 12.87, "lng": -85.21, "score": 50, "programs": ["US OFAC", "EU"]},
    {"country": "Eritrea", "iso": "ER", "lat": 15.18, "lng": 39.78, "score": 60, "programs": ["EU", "US OFAC"]},
    {"country": "Libya", "iso": "LY", "lat": 26.34, "lng": 17.23, "score": 55, "programs": ["UN", "US OFAC", "EU"]},
    {"country": "Iraq", "iso": "IQ", "lat": 33.22, "lng": 43.68, "score": 35, "programs": ["UN legacy"]},
    {"country": "Lebanon (Hezb)", "iso": "LB", "lat": 33.85, "lng": 35.86, "score": 45, "programs": ["US OFAC sectoral"]},
    {"country": "Yemen (Houthis)", "iso": "YE", "lat": 15.55, "lng": 48.52, "score": 60, "programs": ["UN", "US OFAC"]},
    {"country": "Zimbabwe", "iso": "ZW", "lat": -19.02, "lng": 29.15, "score": 35, "programs": ["US OFAC", "EU"]},
    {"country": "C. African Rep", "iso": "CF", "lat": 6.61, "lng": 20.94, "score": 40, "programs": ["UN", "US OFAC"]},
]


# ═══════════════ LIVE STRATEGIC WEBCAMS ═══════════════
LIVE_WEBCAMS = [
    {"name": "Times Square NYC", "country": "US", "lat": 40.758, "lng": -73.985, "category": "city", "url": "https://www.earthcam.com/usa/newyork/timessquare/"},
    {"name": "White House", "country": "US", "lat": 38.898, "lng": -77.037, "category": "political", "url": "https://www.earthcam.com/usa/dc/whitehouse/"},
    {"name": "Trafalgar Square London", "country": "GB", "lat": 51.508, "lng": -0.128, "category": "city", "url": "https://www.skylinewebcams.com/en/webcam/united-kingdom/england/london.html"},
    {"name": "Tokyo Shibuya Crossing", "country": "JP", "lat": 35.660, "lng": 139.700, "category": "city", "url": "https://www.skylinewebcams.com/en/webcam/japan/tokyo.html"},
    {"name": "Red Square Moscow", "country": "RU", "lat": 55.754, "lng": 37.620, "category": "political", "url": "https://moscowtv.ru/webcam/"},
    {"name": "Tiananmen Square", "country": "CN", "lat": 39.906, "lng": 116.391, "category": "political", "url": "https://www.beijing-cam.com/"},
    {"name": "Hong Kong Harbor", "country": "HK", "lat": 22.295, "lng": 114.169, "category": "city", "url": "https://www.skylinewebcams.com/en/webcam/china/hong-kong.html"},
    {"name": "Suez Canal Approach", "country": "EG", "lat": 30.580, "lng": 32.350, "category": "shipping", "url": "https://www.shipspotting.com/"},
    {"name": "Panama Canal", "country": "PA", "lat": 9.082, "lng": -79.679, "category": "shipping", "url": "https://www.pancanal.com/"},
    {"name": "Bosphorus Strait", "country": "TR", "lat": 41.045, "lng": 29.034, "category": "shipping", "url": "https://www.skylinewebcams.com/en/webcam/turkey/istanbul.html"},
    {"name": "Etna Volcano", "country": "IT", "lat": 37.751, "lng": 14.994, "category": "volcano", "url": "https://www.skylinewebcams.com/en/webcam/italia/sicilia/catania/etna.html"},
    {"name": "Stromboli", "country": "IT", "lat": 38.789, "lng": 15.213, "category": "volcano", "url": "https://www.skylinewebcams.com/en/webcam/italia/sicilia/messina/stromboli.html"},
    {"name": "Sakurajima Volcano", "country": "JP", "lat": 31.585, "lng": 130.657, "category": "volcano", "url": "https://www.youtube.com/results?search_query=sakurajima+live"},
    {"name": "Old Faithful Yellowstone", "country": "US", "lat": 44.460, "lng": -110.828, "category": "nature", "url": "https://www.nps.gov/yell/learn/photosmultimedia/webcams.htm"},
    {"name": "Niagara Falls", "country": "US", "lat": 43.082, "lng": -79.071, "category": "nature", "url": "https://www.earthcam.com/usa/newyork/niagarafalls/"},
    {"name": "ISS Live", "country": "Orbital", "lat": 0.0, "lng": 0.0, "category": "space", "url": "https://www.nasa.gov/multimedia/nasatv/iss_ustream.html"},
]


# ═══════════════ APT GROUPS (Cyber Threat Actors) ═══════════════
APT_GROUPS = [
    {"id": "APT28", "name": "APT28 (Fancy Bear)", "sponsor": "RU", "agency": "GRU Unit 26165", "lat": 55.755, "lng": 37.617, "targets": "Govt, military, NATO, election infra", "first_seen": 2008, "severity": "CRITICAL"},
    {"id": "APT29", "name": "APT29 (Cozy Bear / Midnight Blizzard)", "sponsor": "RU", "agency": "SVR", "lat": 55.760, "lng": 37.620, "targets": "Govt, think tanks, SolarWinds supply-chain", "first_seen": 2008, "severity": "CRITICAL"},
    {"id": "Sandworm", "name": "Sandworm (Voodoo Bear)", "sponsor": "RU", "agency": "GRU Unit 74455", "lat": 55.770, "lng": 37.610, "targets": "ICS/SCADA, Ukraine power grid, NotPetya", "first_seen": 2009, "severity": "EXTREME"},
    {"id": "APT41", "name": "APT41 (Double Dragon / Wicked Panda)", "sponsor": "CN", "agency": "MSS contractors", "lat": 39.906, "lng": 116.391, "targets": "Healthcare, telecom, supply-chain", "first_seen": 2012, "severity": "HIGH"},
    {"id": "APT40", "name": "APT40 (Leviathan / Kryptonite Panda)", "sponsor": "CN", "agency": "MSS Hainan", "lat": 20.040, "lng": 110.330, "targets": "Maritime, defense, naval", "first_seen": 2013, "severity": "HIGH"},
    {"id": "APT10", "name": "APT10 (Stone Panda / MenuPass)", "sponsor": "CN", "agency": "MSS Tianjin", "lat": 39.343, "lng": 117.361, "targets": "Managed service providers, cloud", "first_seen": 2009, "severity": "HIGH"},
    {"id": "VoltTyphoon", "name": "Volt Typhoon", "sponsor": "CN", "agency": "PLA", "lat": 39.910, "lng": 116.400, "targets": "US critical infra (water, power, comms)", "first_seen": 2021, "severity": "EXTREME"},
    {"id": "FloodTyphoon", "name": "Flax Typhoon", "sponsor": "CN", "agency": "PLA", "lat": 39.911, "lng": 116.405, "targets": "Routers, IoT, edge devices for botnets", "first_seen": 2023, "severity": "HIGH"},
    {"id": "Lazarus", "name": "Lazarus Group", "sponsor": "KP", "agency": "RGB Bureau 121", "lat": 39.020, "lng": 125.750, "targets": "Crypto exchanges, SWIFT, defense", "first_seen": 2009, "severity": "CRITICAL"},
    {"id": "APT37", "name": "APT37 (Reaper / ScarCruft)", "sponsor": "KP", "agency": "RGB", "lat": 39.022, "lng": 125.755, "targets": "South Korea, Japan, defectors", "first_seen": 2012, "severity": "HIGH"},
    {"id": "APT38", "name": "APT38 (BlueNoroff)", "sponsor": "KP", "agency": "Lazarus subgroup", "lat": 39.023, "lng": 125.760, "targets": "Banks, financial heists", "first_seen": 2014, "severity": "HIGH"},
    {"id": "APT33", "name": "APT33 (Elfin / Refined Kitten)", "sponsor": "IR", "agency": "IRGC", "lat": 35.689, "lng": 51.388, "targets": "Aerospace, energy, petrochem", "first_seen": 2013, "severity": "HIGH"},
    {"id": "APT34", "name": "APT34 (OilRig / Helix Kitten)", "sponsor": "IR", "agency": "MOIS", "lat": 35.700, "lng": 51.420, "targets": "Middle East govts, telecom", "first_seen": 2014, "severity": "HIGH"},
    {"id": "APT35", "name": "APT35 (Charming Kitten / Phosphorus)", "sponsor": "IR", "agency": "IRGC IO", "lat": 35.690, "lng": 51.390, "targets": "Journalists, dissidents, govt", "first_seen": 2014, "severity": "HIGH"},
    {"id": "MuddyWater", "name": "MuddyWater (TEMP.Zagros)", "sponsor": "IR", "agency": "MOIS", "lat": 35.710, "lng": 51.430, "targets": "Govt, telecom (Middle East/Asia)", "first_seen": 2017, "severity": "MEDIUM"},
    {"id": "Equation", "name": "Equation Group", "sponsor": "US", "agency": "NSA TAO", "lat": 39.108, "lng": -76.770, "targets": "Foreign govts (attributed by Kaspersky)", "first_seen": 2001, "severity": "EXTREME"},
    {"id": "TurlaSnake", "name": "Turla (Snake / Venomous Bear)", "sponsor": "RU", "agency": "FSB Center 16", "lat": 55.752, "lng": 37.618, "targets": "Govt, embassies, research", "first_seen": 2004, "severity": "HIGH"},
    {"id": "GamaredonAPT", "name": "Gamaredon (Primitive Bear)", "sponsor": "RU", "agency": "FSB", "lat": 44.952, "lng": 34.102, "targets": "Ukraine govt, military", "first_seen": 2013, "severity": "HIGH"},
]


# ═══════════════ NASA FIRMS THERMAL HOTSPOTS ═══════════════
# VIIRS-style fire detection samples (curated, not live API)
NASA_FIRMS_FIRES = [
    {"lat": -3.45, "lng": -62.21, "country": "BR", "frp": 124.5, "confidence": "high", "biome": "Amazon", "detail": "Amazonas state deforestation fire"},
    {"lat": -10.92, "lng": -51.34, "country": "BR", "frp": 89.2, "confidence": "high", "biome": "Cerrado", "detail": "Mato Grosso savanna burn"},
    {"lat": -16.42, "lng": -56.38, "country": "BR", "frp": 156.8, "confidence": "high", "biome": "Pantanal", "detail": "Pantanal wetland mega-fire"},
    {"lat": -2.51, "lng": 23.71, "country": "CD", "frp": 67.3, "confidence": "medium", "biome": "Congo", "detail": "DRC slash-and-burn"},
    {"lat": -12.34, "lng": 31.45, "country": "ZM", "frp": 45.7, "confidence": "medium", "biome": "Miombo", "detail": "Zambia agricultural burn"},
    {"lat": 60.42, "lng": 106.78, "country": "RU", "frp": 234.6, "confidence": "high", "biome": "Boreal", "detail": "Krasnoyarsk taiga megafire"},
    {"lat": 62.18, "lng": 129.56, "country": "RU", "frp": 187.2, "confidence": "high", "biome": "Boreal", "detail": "Yakutia permafrost fire"},
    {"lat": 38.75, "lng": -122.42, "country": "US", "frp": 312.8, "confidence": "high", "biome": "Mediterranean", "detail": "Northern California wildfire"},
    {"lat": 34.21, "lng": -117.85, "country": "US", "frp": 198.4, "confidence": "high", "biome": "Chaparral", "detail": "Southern Cal Santa Ana fire"},
    {"lat": 49.12, "lng": -120.45, "country": "CA", "frp": 145.2, "confidence": "high", "biome": "Boreal", "detail": "BC interior wildfire"},
    {"lat": 56.78, "lng": -111.32, "country": "CA", "frp": 178.9, "confidence": "high", "biome": "Boreal", "detail": "Alberta oil sands region fire"},
    {"lat": -33.92, "lng": 150.55, "country": "AU", "frp": 87.6, "confidence": "medium", "biome": "Eucalypt", "detail": "NSW bushfire"},
    {"lat": 40.18, "lng": 22.45, "country": "GR", "frp": 92.4, "confidence": "high", "biome": "Mediterranean", "detail": "Greek island wildfire"},
    {"lat": 37.92, "lng": -8.12, "country": "PT", "frp": 76.3, "confidence": "high", "biome": "Mediterranean", "detail": "Portugal Algarve fire"},
    {"lat": 36.45, "lng": 4.21, "country": "DZ", "frp": 56.8, "confidence": "medium", "biome": "Mediterranean", "detail": "Kabylie region fire"},
    {"lat": 28.34, "lng": 77.12, "country": "IN", "frp": 67.4, "confidence": "low", "biome": "Agricultural", "detail": "Punjab stubble burning"},
    {"lat": -1.23, "lng": 113.45, "country": "ID", "frp": 142.7, "confidence": "high", "biome": "Peat", "detail": "Kalimantan peat fire"},
    {"lat": 0.21, "lng": 101.78, "country": "ID", "frp": 98.2, "confidence": "high", "biome": "Peat", "detail": "Sumatra haze source"},
    {"lat": 23.45, "lng": -104.82, "country": "MX", "frp": 65.4, "confidence": "medium", "biome": "Pine-oak", "detail": "Sierra Madre fire"},
    {"lat": 11.72, "lng": -1.45, "country": "BF", "frp": 34.2, "confidence": "low", "biome": "Sahel", "detail": "Burkina Faso savanna burn"},
]


# ═══════════════ AVIATION INTELLIGENCE ═══════════════
AVIATION_AIRPORTS = [
    {"iata": "JFK", "name": "JFK New York", "country": "US", "lat": 40.641, "lng": -73.778, "delay_min": 24, "delay_status": "MODERATE", "ground_stop": False, "notams": 4, "passengers_m": 62.5},
    {"iata": "LAX", "name": "LA International", "country": "US", "lat": 33.942, "lng": -118.408, "delay_min": 31, "delay_status": "MODERATE", "ground_stop": False, "notams": 3, "passengers_m": 75.1},
    {"iata": "ORD", "name": "Chicago O'Hare", "country": "US", "lat": 41.978, "lng": -87.904, "delay_min": 67, "delay_status": "SEVERE", "ground_stop": True, "notams": 7, "passengers_m": 73.9},
    {"iata": "ATL", "name": "Atlanta Hartsfield-Jackson", "country": "US", "lat": 33.640, "lng": -84.428, "delay_min": 18, "delay_status": "MINOR", "ground_stop": False, "notams": 2, "passengers_m": 93.7},
    {"iata": "DFW", "name": "Dallas/Fort Worth", "country": "US", "lat": 32.897, "lng": -97.038, "delay_min": 22, "delay_status": "MINOR", "ground_stop": False, "notams": 3, "passengers_m": 73.4},
    {"iata": "DEN", "name": "Denver", "country": "US", "lat": 39.862, "lng": -104.673, "delay_min": 45, "delay_status": "MAJOR", "ground_stop": False, "notams": 5, "passengers_m": 77.8},
    {"iata": "SFO", "name": "San Francisco", "country": "US", "lat": 37.622, "lng": -122.375, "delay_min": 38, "delay_status": "MAJOR", "ground_stop": False, "notams": 4, "passengers_m": 50.0},
    {"iata": "LHR", "name": "London Heathrow", "country": "GB", "lat": 51.470, "lng": -0.454, "delay_min": 28, "delay_status": "MODERATE", "ground_stop": False, "notams": 3, "passengers_m": 79.2},
    {"iata": "CDG", "name": "Paris CDG", "country": "FR", "lat": 49.010, "lng": 2.548, "delay_min": 19, "delay_status": "MINOR", "ground_stop": False, "notams": 2, "passengers_m": 67.4},
    {"iata": "FRA", "name": "Frankfurt", "country": "DE", "lat": 50.038, "lng": 8.562, "delay_min": 21, "delay_status": "MINOR", "ground_stop": False, "notams": 4, "passengers_m": 60.7},
    {"iata": "AMS", "name": "Amsterdam Schiphol", "country": "NL", "lat": 52.310, "lng": 4.768, "delay_min": 26, "delay_status": "MODERATE", "ground_stop": False, "notams": 3, "passengers_m": 71.7},
    {"iata": "DXB", "name": "Dubai Intl", "country": "AE", "lat": 25.253, "lng": 55.365, "delay_min": 12, "delay_status": "MINOR", "ground_stop": False, "notams": 2, "passengers_m": 86.9},
    {"iata": "HND", "name": "Tokyo Haneda", "country": "JP", "lat": 35.549, "lng": 139.779, "delay_min": 8, "delay_status": "MINOR", "ground_stop": False, "notams": 1, "passengers_m": 87.0},
    {"iata": "PEK", "name": "Beijing Capital", "country": "CN", "lat": 40.080, "lng": 116.585, "delay_min": 35, "delay_status": "MODERATE", "ground_stop": False, "notams": 3, "passengers_m": 100.0},
    {"iata": "PVG", "name": "Shanghai Pudong", "country": "CN", "lat": 31.143, "lng": 121.805, "delay_min": 41, "delay_status": "MAJOR", "ground_stop": False, "notams": 4, "passengers_m": 76.2},
    {"iata": "ICN", "name": "Seoul Incheon", "country": "KR", "lat": 37.469, "lng": 126.450, "delay_min": 14, "delay_status": "MINOR", "ground_stop": False, "notams": 2, "passengers_m": 71.2},
    {"iata": "SIN", "name": "Singapore Changi", "country": "SG", "lat": 1.359, "lng": 103.989, "delay_min": 9, "delay_status": "MINOR", "ground_stop": False, "notams": 1, "passengers_m": 68.3},
    {"iata": "DOH", "name": "Doha Hamad", "country": "QA", "lat": 25.273, "lng": 51.608, "delay_min": 11, "delay_status": "MINOR", "ground_stop": False, "notams": 1, "passengers_m": 45.9},
    {"iata": "IST", "name": "Istanbul Airport", "country": "TR", "lat": 41.275, "lng": 28.752, "delay_min": 33, "delay_status": "MODERATE", "ground_stop": False, "notams": 4, "passengers_m": 76.1},
    {"iata": "TLV", "name": "Tel Aviv Ben Gurion", "country": "IL", "lat": 32.011, "lng": 34.887, "delay_min": 52, "delay_status": "SEVERE", "ground_stop": False, "notams": 9, "passengers_m": 24.8},
    {"iata": "BEY", "name": "Beirut Rafic Hariri", "country": "LB", "lat": 33.821, "lng": 35.488, "delay_min": 78, "delay_status": "SEVERE", "ground_stop": True, "notams": 12, "passengers_m": 8.0},
    {"iata": "DAM", "name": "Damascus Intl", "country": "SY", "lat": 33.411, "lng": 36.514, "delay_min": 0, "delay_status": "CLOSED", "ground_stop": True, "notams": 14, "passengers_m": 0.0},
    {"iata": "TGD", "name": "Podgorica (Balkans)", "country": "ME", "lat": 42.359, "lng": 19.252, "delay_min": 15, "delay_status": "MINOR", "ground_stop": False, "notams": 2, "passengers_m": 1.8},
    {"iata": "DME", "name": "Moscow Domodedovo", "country": "RU", "lat": 55.408, "lng": 37.906, "delay_min": 47, "delay_status": "MAJOR", "ground_stop": False, "notams": 6, "passengers_m": 19.5},
    {"iata": "KBP", "name": "Kyiv Boryspil", "country": "UA", "lat": 50.345, "lng": 30.894, "delay_min": 0, "delay_status": "CLOSED", "ground_stop": True, "notams": 18, "passengers_m": 0.0},
]

NOTAM_CLOSURES = [
    {"airport": "DAM", "type": "CLOSED", "reason": "Airspace conflict; civilian operations suspended", "since": "2024-12-08"},
    {"airport": "KBP", "type": "CLOSED", "reason": "Russian invasion; airspace closed since 2022-02-24", "since": "2022-02-24"},
    {"airport": "BEY", "type": "RESTRICTED", "reason": "Israel-Lebanon escalation; intermittent ground stops", "since": "2024-09-23"},
    {"airport": "SHA", "type": "RESTRICTED", "reason": "PLA exercises; airspace congestion", "since": "2026-03-15"},
    {"airport": "EVN", "type": "RESTRICTED", "reason": "Caucasus tensions", "since": "2024-08-12"},
    {"airport": "TLV", "type": "RESTRICTED", "reason": "Iron Dome launches; periodic closures", "since": "2024-04-13"},
    {"airport": "MNL", "type": "RESTRICTED", "reason": "Typhoon Pepito approach", "since": "2026-04-05"},
    {"airport": "TPE", "type": "RESTRICTED", "reason": "PLA missile drills around Taiwan Strait", "since": "2026-03-22"},
    {"airport": "MMD", "type": "CLOSED", "reason": "Volcanic ash from Mount Marapi", "since": "2026-04-01"},
    {"airport": "SVX", "type": "RESTRICTED", "reason": "Unspecified military activity", "since": "2026-03-30"},
]


# ═══════════════ CLIMATE ANOMALIES (ERA5-style baseline deviations) ═══════════════
CLIMATE_ANOMALIES = [
    {"zone": "Arctic", "lat": 80.0, "lng": 0.0, "temp_anom_c": 7.4, "precip_anom_pct": 18, "severity": "EXTREME", "detail": "Polar amplification 4× global avg"},
    {"zone": "West Antarctica", "lat": -78.0, "lng": -100.0, "temp_anom_c": 3.2, "precip_anom_pct": -5, "severity": "HIGH", "detail": "Thwaites/Pine Island accelerating"},
    {"zone": "Greenland", "lat": 72.0, "lng": -42.0, "temp_anom_c": 4.8, "precip_anom_pct": 12, "severity": "HIGH", "detail": "Surface melt extent record-breaking"},
    {"zone": "Western Europe", "lat": 47.0, "lng": 5.0, "temp_anom_c": 2.1, "precip_anom_pct": -22, "severity": "HIGH", "detail": "Persistent drought; aquifer depletion"},
    {"zone": "Mediterranean", "lat": 38.0, "lng": 15.0, "temp_anom_c": 2.6, "precip_anom_pct": -28, "severity": "HIGH", "detail": "Heat dome events; agriculture stressed"},
    {"zone": "Sahel", "lat": 14.0, "lng": 0.0, "temp_anom_c": 1.8, "precip_anom_pct": -15, "severity": "MEDIUM", "detail": "Desertification accelerating south"},
    {"zone": "Horn of Africa", "lat": 5.0, "lng": 40.0, "temp_anom_c": 2.2, "precip_anom_pct": -32, "severity": "EXTREME", "detail": "5+ failed rainy seasons"},
    {"zone": "Amazon Basin", "lat": -5.0, "lng": -60.0, "temp_anom_c": 1.9, "precip_anom_pct": -35, "severity": "EXTREME", "detail": "Drought + deforestation tipping risk"},
    {"zone": "Pantanal", "lat": -17.0, "lng": -56.0, "temp_anom_c": 2.4, "precip_anom_pct": -40, "severity": "EXTREME", "detail": "Wetland 30% burned in 2024"},
    {"zone": "South Asia", "lat": 22.0, "lng": 80.0, "temp_anom_c": 1.5, "precip_anom_pct": 8, "severity": "MEDIUM", "detail": "Monsoon variability increasing"},
    {"zone": "Australian Outback", "lat": -25.0, "lng": 135.0, "temp_anom_c": 1.7, "precip_anom_pct": -18, "severity": "MEDIUM", "detail": "Fire danger index elevated"},
    {"zone": "Pacific NW (USA)", "lat": 47.0, "lng": -122.0, "temp_anom_c": 2.0, "precip_anom_pct": -10, "severity": "MEDIUM", "detail": "Heat domes; salmon run collapse"},
    {"zone": "California", "lat": 36.0, "lng": -120.0, "temp_anom_c": 1.6, "precip_anom_pct": -25, "severity": "HIGH", "detail": "Megadrought 23+ years"},
    {"zone": "East Africa", "lat": -3.0, "lng": 36.0, "temp_anom_c": 1.4, "precip_anom_pct": -20, "severity": "MEDIUM", "detail": "Glacier retreat on Kilimanjaro"},
    {"zone": "Southeast Asia", "lat": 5.0, "lng": 110.0, "temp_anom_c": 1.3, "precip_anom_pct": 15, "severity": "MEDIUM", "detail": "Typhoon intensification"},
]


# ═══════════════ WTO TRADE POLICY (Active Restrictions) ═══════════════
WTO_TRADE_RESTRICTIONS = [
    {"id": "WTO-2026-EU-CN-EV", "from_country": "EU", "to_country": "CN", "type": "TARIFF", "product": "Electric vehicles", "rate_pct": 38.1, "since": "2024-10", "status": "ACTIVE", "detail": "Anti-subsidy duties on Chinese-made EVs"},
    {"id": "WTO-2026-US-CN-SEMI", "from_country": "US", "to_country": "CN", "type": "EXPORT_CONTROL", "product": "Advanced semiconductors", "rate_pct": 100, "since": "2022-10", "status": "ACTIVE", "detail": "Export restrictions on EUV/sub-7nm chips, AI accelerators"},
    {"id": "WTO-2026-CN-US-RARE", "from_country": "CN", "to_country": "US", "type": "EXPORT_CONTROL", "product": "Gallium, germanium, rare earths", "rate_pct": 100, "since": "2023-08", "status": "ACTIVE", "detail": "Critical minerals counter-restrictions"},
    {"id": "WTO-2026-US-MX-STEEL", "from_country": "US", "to_country": "MX", "type": "TARIFF", "product": "Steel, aluminum", "rate_pct": 25, "since": "2024-07", "status": "ACTIVE", "detail": "Section 232 reinstated"},
    {"id": "WTO-2026-IN-CN-SOLAR", "from_country": "IN", "to_country": "CN", "type": "TARIFF", "product": "Solar modules", "rate_pct": 40, "since": "2024-04", "status": "ACTIVE", "detail": "Anti-dumping safeguard"},
    {"id": "WTO-2026-EU-RU-OIL", "from_country": "EU", "to_country": "RU", "type": "EMBARGO", "product": "Crude oil, refined products", "rate_pct": 100, "since": "2022-12", "status": "ACTIVE", "detail": "G7 price cap + EU embargo"},
    {"id": "WTO-2026-US-RU-TECH", "from_country": "US", "to_country": "RU", "type": "EXPORT_CONTROL", "product": "Dual-use tech, chips", "rate_pct": 100, "since": "2022-02", "status": "ACTIVE", "detail": "Severe export controls"},
    {"id": "WTO-2026-AU-CN-WINE", "from_country": "CN", "to_country": "AU", "type": "TARIFF", "product": "Wine", "rate_pct": 0, "since": "2024-03", "status": "LIFTED", "detail": "Anti-dumping duties removed after 3 years"},
    {"id": "WTO-2026-BR-AR-AGRI", "from_country": "BR", "to_country": "AR", "type": "TARIFF", "product": "Wheat, soy", "rate_pct": 12, "since": "2024-09", "status": "ACTIVE", "detail": "Mercosur dispute"},
    {"id": "WTO-2026-EU-BR-DEFOR", "from_country": "EU", "to_country": "BR", "type": "REGULATION", "product": "Beef, soy, palm oil", "rate_pct": 0, "since": "2025-01", "status": "ACTIVE", "detail": "EUDR deforestation regulation"},
    {"id": "WTO-2026-TR-EU-CITRUS", "from_country": "EU", "to_country": "TR", "type": "SPS", "product": "Citrus, vegetables", "rate_pct": 0, "since": "2024-11", "status": "ACTIVE", "detail": "Pesticide residue findings"},
    {"id": "WTO-2026-KR-JP-CHEM", "from_country": "JP", "to_country": "KR", "type": "EXPORT_CONTROL", "product": "Hydrogen fluoride, photoresists", "rate_pct": 0, "since": "2019-07", "status": "EASED", "detail": "Eased in 2023 after détente"},
    {"id": "WTO-2026-IN-US-DAIRY", "from_country": "IN", "to_country": "US", "type": "TARIFF", "product": "Dairy, almonds", "rate_pct": 70, "since": "2019-06", "status": "ACTIVE", "detail": "Retaliation over GSP withdrawal"},
    {"id": "WTO-2026-VN-US-FUR", "from_country": "US", "to_country": "VN", "type": "TARIFF", "product": "Furniture, plywood", "rate_pct": 22, "since": "2024-12", "status": "ACTIVE", "detail": "Anti-dumping; transshipment from China"},
]


# ═══════════════ BIS / CENTRAL BANK POLICY RATES ═══════════════
BIS_POLICY_RATES = [
    {"bank": "Federal Reserve", "country": "US", "rate_pct": 4.25, "delta_bps": -25, "next_meeting": "2026-05-01", "stance": "DOVISH", "balance_sheet_t": 6.8},
    {"bank": "European Central Bank", "country": "EU", "rate_pct": 2.50, "delta_bps": -25, "next_meeting": "2026-04-17", "stance": "DOVISH", "balance_sheet_t": 6.4},
    {"bank": "Bank of England", "country": "GB", "rate_pct": 4.00, "delta_bps": -25, "next_meeting": "2026-05-08", "stance": "NEUTRAL", "balance_sheet_t": 0.7},
    {"bank": "Bank of Japan", "country": "JP", "rate_pct": 0.50, "delta_bps": 25, "next_meeting": "2026-04-30", "stance": "HAWKISH", "balance_sheet_t": 5.2},
    {"bank": "People's Bank of China", "country": "CN", "rate_pct": 3.10, "delta_bps": -10, "next_meeting": "2026-04-21", "stance": "DOVISH", "balance_sheet_t": 6.1},
    {"bank": "Swiss National Bank", "country": "CH", "rate_pct": 0.25, "delta_bps": -25, "next_meeting": "2026-06-19", "stance": "DOVISH", "balance_sheet_t": 0.9},
    {"bank": "Bank of Canada", "country": "CA", "rate_pct": 2.75, "delta_bps": -25, "next_meeting": "2026-04-16", "stance": "DOVISH", "balance_sheet_t": 0.4},
    {"bank": "Reserve Bank of Australia", "country": "AU", "rate_pct": 4.00, "delta_bps": -25, "next_meeting": "2026-05-20", "stance": "NEUTRAL", "balance_sheet_t": 0.4},
    {"bank": "Reserve Bank of India", "country": "IN", "rate_pct": 6.00, "delta_bps": -25, "next_meeting": "2026-06-06", "stance": "NEUTRAL", "balance_sheet_t": 0.8},
    {"bank": "Banco Central do Brasil", "country": "BR", "rate_pct": 14.25, "delta_bps": 100, "next_meeting": "2026-05-07", "stance": "HAWKISH", "balance_sheet_t": 0.3},
    {"bank": "Bank of Korea", "country": "KR", "rate_pct": 2.75, "delta_bps": -25, "next_meeting": "2026-04-24", "stance": "DOVISH", "balance_sheet_t": 0.5},
    {"bank": "Banco de México", "country": "MX", "rate_pct": 9.00, "delta_bps": -50, "next_meeting": "2026-05-15", "stance": "DOVISH", "balance_sheet_t": 0.2},
    {"bank": "Norges Bank", "country": "NO", "rate_pct": 4.25, "delta_bps": -25, "next_meeting": "2026-05-08", "stance": "NEUTRAL", "balance_sheet_t": 0.1},
    {"bank": "Riksbank", "country": "SE", "rate_pct": 2.25, "delta_bps": -25, "next_meeting": "2026-05-08", "stance": "DOVISH", "balance_sheet_t": 0.1},
]


# ═══════════════ MARKET DATA — SECTORS, OIL, BTC ETF, STABLECOINS ═══════════════
SECTOR_HEATMAP = [
    {"ticker": "XLK", "name": "Technology", "change_pct": 1.42, "ytd_pct": 12.4, "weight_pct": 30.8},
    {"ticker": "XLF", "name": "Financials", "change_pct": 0.68, "ytd_pct": 8.2, "weight_pct": 13.5},
    {"ticker": "XLV", "name": "Health Care", "change_pct": -0.45, "ytd_pct": 4.1, "weight_pct": 11.7},
    {"ticker": "XLY", "name": "Consumer Discretionary", "change_pct": 0.92, "ytd_pct": 9.6, "weight_pct": 10.8},
    {"ticker": "XLC", "name": "Communication Services", "change_pct": 1.18, "ytd_pct": 14.2, "weight_pct": 9.2},
    {"ticker": "XLI", "name": "Industrials", "change_pct": 0.34, "ytd_pct": 6.8, "weight_pct": 8.4},
    {"ticker": "XLP", "name": "Consumer Staples", "change_pct": -0.12, "ytd_pct": 2.3, "weight_pct": 6.1},
    {"ticker": "XLE", "name": "Energy", "change_pct": -1.65, "ytd_pct": -8.4, "weight_pct": 3.6},
    {"ticker": "XLU", "name": "Utilities", "change_pct": 0.21, "ytd_pct": 5.7, "weight_pct": 2.5},
    {"ticker": "XLB", "name": "Materials", "change_pct": -0.78, "ytd_pct": -2.1, "weight_pct": 2.3},
    {"ticker": "XLRE", "name": "Real Estate", "change_pct": 0.45, "ytd_pct": 3.4, "weight_pct": 2.2},
]

OIL_ANALYTICS = {
    "wti_spot": 64.20, "wti_change_pct": -0.85, "wti_ytd_pct": -8.2,
    "brent_spot": 68.10, "brent_change_pct": -0.72, "brent_ytd_pct": -7.4,
    "us_production_mbd": 13.45, "us_production_change": -0.12,
    "us_inventory_mb": 442.3, "us_inventory_change": 2.8,
    "spr_mb": 393.6, "spr_change": 0.4,
    "rig_count": 581, "rig_count_change": -3,
    "opec_production_mbd": 27.18, "non_opec_production_mbd": 53.42,
    "demand_2026e_mbd": 103.8, "supply_2026e_mbd": 104.2,
    "spread_brent_wti": 3.90,
    "detail": "Soft demand outlook + OPEC+ unwind keeping prices range-bound",
}


# ═══════════════ WORLD ENERGY (electricity production + transit) ═══════════════
#
# Snapshot of world electricity generation, regional mix, transit chokepoints,
# LNG export hubs, renewable-share forecast, and energy-sector derivatives.
# Data is annual-cadence (Ember/Energy Institute via OWID) refreshed by hand
# during release cycles. Live overlay endpoints (Polymarket, Stooq) are
# called separately via fetch_world_energy_live() and merged on /api/all.

WORLD_ENERGY_MIX = {
    "year": 2025,
    "world_total_twh": 31772,
    "breakdown": [  # ordered descending by share — colors match across the suite
        {"source": "Coal",     "twh": 10472, "share": 0.330, "color": "#6b7280"},
        {"source": "Gas",      "twh":  6926, "share": 0.218, "color": "#a78bfa"},
        {"source": "Hydro",    "twh":  4448, "share": 0.140, "color": "#3b82f6"},
        {"source": "Nuclear",  "twh":  2812, "share": 0.088, "color": "#22d3ee"},
        {"source": "Solar",    "twh":  2779, "share": 0.087, "color": "#fbbf24"},
        {"source": "Wind",     "twh":  2713, "share": 0.085, "color": "#34d399"},
        {"source": "Oil",      "twh":   826, "share": 0.026, "color": "#9ca3af"},
        {"source": "Bioenergy","twh":   698, "share": 0.022, "color": "#84cc16"},
        {"source": "Other RE", "twh":    98, "share": 0.003, "color": "#f472b6"},
    ],
    "share_renewable": 0.338,
    "share_renewable_10y_delta_pp": 10.79,
    "share_fossil": 0.574,
    "share_fossil_10y_delta_pp": -9.12,
    "share_low_carbon": 0.426,
    "share_nuclear": 0.088,
    "growth_multipliers_10y": {"Solar": 10.9, "Wind": 3.3, "Coal": 1.13},
    "source": "Our World in Data — Ember + Energy Institute Statistical Review",
}

WORLD_ENERGY_REGIONS = [
    {"region": "Africa",        "year": 2024, "twh":   962, "share_renewable": 0.247, "share_low_carbon": 0.255, "carbon_intensity_g": 544, "top": [("Gas",0.42),("Coal",0.25),("Hydro",0.17)]},
    {"region": "Asia",          "year": 2024, "twh": 18015, "share_renewable": 0.273, "share_low_carbon": 0.321, "carbon_intensity_g": 573, "top": [("Coal",0.49),("Gas",0.16),("Hydro",0.12)]},
    {"region": "Europe",        "year": 2025, "twh":  4626, "share_renewable": 0.416, "share_low_carbon": 0.620, "carbon_intensity_g": 271, "top": [("Gas",0.24),("Nuclear",0.20),("Hydro",0.16)]},
    {"region": "North America", "year": 2024, "twh":  5534, "share_renewable": 0.288, "share_low_carbon": 0.447, "carbon_intensity_g": 369, "top": [("Gas",0.40),("Nuclear",0.16),("Coal",0.13)]},
    {"region": "South America", "year": 2024, "twh":  1321, "share_renewable": 0.768, "share_low_carbon": 0.788, "carbon_intensity_g": 167, "top": [("Hydro",0.53),("Gas",0.15),("Wind",0.11)]},
    {"region": "Oceania",       "year": 2024, "twh":   338, "share_renewable": 0.414, "share_low_carbon": 0.414, "carbon_intensity_g": 495, "top": [("Coal",0.38),("Gas",0.16),("Solar",0.15)]},
    {"region": "Middle East",   "year": 2025, "twh":  1582, "share_renewable": 0.051, "share_low_carbon": 0.081, "carbon_intensity_g": 635, "top": [("Gas",0.73),("Oil",0.18),("Solar",0.04)]},
]

WORLD_ENERGY_TOP_PRODUCERS = [
    {"country": "China",         "twh": 10583, "share_renewable": 0.370, "carbon_g": 525},
    {"country": "United States", "twh":  4520, "share_renewable": 0.256, "carbon_g": 384},
    {"country": "India",         "twh":  2082, "share_renewable": 0.240, "carbon_g": 670},
    {"country": "Russia",        "twh":  1193, "share_renewable": 0.170, "carbon_g": 450},
    {"country": "Japan",         "twh":  1030, "share_renewable": 0.236, "carbon_g": 477},
    {"country": "Brazil",        "twh":   751, "share_renewable": 0.866, "carbon_g": 110},
    {"country": "Canada",        "twh":   652, "share_renewable": 0.640, "carbon_g": 191},
    {"country": "South Korea",   "twh":   625, "share_renewable": 0.099, "carbon_g": 417},
    {"country": "France",        "twh":   570, "share_renewable": 0.261, "carbon_g":  41},
    {"country": "Germany",       "twh":   500, "share_renewable": 0.591, "carbon_g": 330},
    {"country": "Iran",          "twh":   396, "share_renewable": 0.040, "carbon_g": 660},
    {"country": "Mexico",        "twh":   357, "share_renewable": 0.231, "carbon_g": 474},
    {"country": "Turkey",        "twh":   354, "share_renewable": 0.433, "carbon_g": 475},
    {"country": "Vietnam",       "twh":   310, "share_renewable": 0.454, "carbon_g": 461},
    {"country": "United Kingdom","twh":   292, "share_renewable": 0.520, "carbon_g": 217},
    {"country": "Spain",         "twh":   288, "share_renewable": 0.559, "carbon_g": 154},
    {"country": "Australia",     "twh":   286, "share_renewable": 0.386, "carbon_g": 525},
    {"country": "Italy",         "twh":   265, "share_renewable": 0.488, "carbon_g": 285},
    {"country": "South Africa",  "twh":   243, "share_renewable": 0.136, "carbon_g": 699},
    {"country": "Poland",        "twh":   173, "share_renewable": 0.315, "carbon_g": 589},
]

# Maritime chokepoints — energy throughput emphasis. Augments the geopolitical
# STRATEGIC_CHOKEPOINTS list above; this one carries petroleum/LNG specifics
# and the analytical context. Sourced from EIA "World Oil Transit Chokepoints"
# (latest cycle) + GIIGNL LNG annual report.
ENERGY_CHOKEPOINTS = [
    {
        "id": "hormuz", "name": "Strait of Hormuz", "type": "strait",
        "lat": 26.6, "lng": 56.3,
        "between": "Iran / Oman / UAE",
        "oil_mbd": 20.9, "share_seaborne_oil": 0.27, "share_global_lng": 0.20,
        "carries": "Crude + condensate + LNG out of the Persian Gulf",
        "blurb": "World's #1 oil chokepoint. ~21 mb/d of petroleum liquids, ~20% of global LNG. No practical bypass at full volume — Saudi East-West and UAE Habshan-Fujairah pipelines combined cover only ~2.6 mb/d.",
        "risk": "Iran has threatened closure repeatedly (2011-12, 2019, 2024). US 5th Fleet (Bahrain) maintains transit security.",
        "risk_tier": "EXTREME",
    },
    {
        "id": "malacca", "name": "Strait of Malacca", "type": "strait",
        "lat": 2.5, "lng": 101.5,
        "between": "Malaysia / Singapore / Indonesia",
        "oil_mbd": 23.7, "share_seaborne_oil": 0.30, "share_global_lng": None,
        "carries": "Crude + products to East Asia (China, Japan, S Korea)",
        "blurb": "Busiest oil chokepoint by volume. Primary route for Middle Eastern and African crude bound for Northeast Asia. ~30% of seaborne oil and ~33% of all global maritime trade by tonnage.",
        "risk": "Piracy concern (declined post-2010). Strategic vulnerability for China — drives 'Malacca Dilemma' rationale for BRI alternatives (Myanmar pipeline, Pakistan corridor).",
        "risk_tier": "HIGH",
    },
    {
        "id": "suez", "name": "Suez Canal + SUMED Pipeline", "type": "canal",
        "lat": 30.5, "lng": 32.3,
        "between": "Egypt — Mediterranean / Red Sea",
        "oil_mbd": 9.2, "share_seaborne_oil": 0.12, "share_global_lng": None,
        "carries": "Crude + products + LNG between Europe and Asia/Gulf",
        "blurb": "Joint Suez (canal) + SUMED (pipeline bypass) carry ~9.2 mb/d. Houthi attacks since late 2023 collapsed Red Sea transit ~70%, rerouting via Cape of Good Hope (+10-14 days, ~$1M extra fuel per voyage).",
        "risk": "Houthi missile/drone attacks ongoing as of 2025. Ever Given grounding (2021) blocked transit for 6 days, $9B-$10B/day in held cargo.",
        "risk_tier": "EXTREME",
    },
    {
        "id": "bab_el_mandeb", "name": "Bab el-Mandeb", "type": "strait",
        "lat": 12.6, "lng": 43.4,
        "between": "Yemen / Djibouti / Eritrea",
        "oil_mbd": 6.2, "share_seaborne_oil": 0.08, "share_global_lng": None,
        "carries": "Crude + products + LNG between Suez and Indian Ocean",
        "blurb": "Southern gateway to the Red Sea. Pre-2024 ~6.2 mb/d; Houthi attacks have cut transit ~70% since Nov 2023. Most large-tanker traffic now reroutes around Africa.",
        "risk": "Houthi anti-ship missiles + drones. Operation Prosperity Guardian (US-led) provides limited escort.",
        "risk_tier": "EXTREME",
    },
    {
        "id": "turkish_straits", "name": "Turkish Straits (Bosporus + Dardanelles)", "type": "strait",
        "lat": 41.0, "lng": 29.0,
        "between": "Turkey — Black Sea / Mediterranean",
        "oil_mbd": 3.0, "share_seaborne_oil": 0.04, "share_global_lng": None,
        "carries": "Russian + Caspian crude + grain exports to Mediterranean",
        "blurb": "Two narrow straits (Bosporus + Dardanelles) governed by 1936 Montreux Convention. ~3 mb/d crude. Tanker queues common. Critical for Russian Black Sea fleet basing and Caspian crude (Novorossiysk, Tuapse).",
        "risk": "Sanctions enforcement on Russia post-2022 has slowed transit. Turkey can restrict warship passage during conflict (invoked 2022 against Russia + NATO).",
        "risk_tier": "HIGH",
    },
    {
        "id": "panama", "name": "Panama Canal", "type": "canal",
        "lat": 9.1, "lng": -79.7,
        "between": "Panama — Atlantic / Pacific",
        "oil_mbd": 1.0, "share_seaborne_oil": 0.01, "share_global_lng": None,
        "carries": "Mostly products (gasoline, diesel) + some LPG/LNG",
        "blurb": "Only ~1 mb/d of petroleum, but disproportionately important for US Gulf → Asia products trade. 2023-24 drought cut transit slots by ~36%, forced reroutes via Cape Horn / Suez.",
        "risk": "Climate-driven freshwater shortage (Lake Gatún) — recurring drought events expected to worsen. Trump admin reopened question of US control rights in 2025.",
        "risk_tier": "MEDIUM",
    },
    {
        "id": "danish_straits", "name": "Danish Straits", "type": "strait",
        "lat": 56.0, "lng": 11.0,
        "between": "Denmark / Sweden — Baltic / North Sea",
        "oil_mbd": 3.2, "share_seaborne_oil": 0.04, "share_global_lng": None,
        "carries": "Russian Baltic crude (Primorsk, Ust-Luga) to global markets",
        "blurb": "Pre-war ~3.2 mb/d of mostly Russian crude. Post-Ukraine sanctions, traffic increasingly 'shadow fleet' tankers. Denmark + EU exploring inspection regimes for environmental + sanctions enforcement.",
        "risk": "Shadow fleet (uninsured, aging) tankers raise oil-spill risk. Repeated Russian undersea cable + pipeline incidents in Baltic since 2022.",
        "risk_tier": "MEDIUM",
    },
    {
        "id": "cape_good_hope", "name": "Cape of Good Hope", "type": "cape",
        "lat": -34.4, "lng": 18.5,
        "between": "South Africa — Suez bypass route",
        "oil_mbd": 7.5, "share_seaborne_oil": 0.10, "share_global_lng": None,
        "carries": "Reroute path for Suez avoiders",
        "blurb": "Volume jumped from ~3 mb/d to ~7.5 mb/d in 2024 as Red Sea transit collapsed. Adds ~10-14 days vs Suez routing. No physical chokepoint risk but vulnerable in any global naval conflict.",
        "risk": "South African ports (Cape Town, Durban) suffer chronic congestion + load-shedding.",
        "risk_tier": "LOW",
    },
]

# Major LNG export hubs — capacity in million tonnes per annum (mtpa).
ENERGY_LNG_HUBS = [
    {"name": "Ras Laffan",     "country": "QA", "lat": 25.9, "lng":  51.5, "mtpa": 77, "role": "Export"},
    {"name": "Sabine Pass",    "country": "US", "lat": 29.7, "lng": -93.9, "mtpa": 30, "role": "Export"},
    {"name": "Corpus Christi", "country": "US", "lat": 27.8, "lng": -97.1, "mtpa": 22, "role": "Export"},
    {"name": "Freeport LNG",   "country": "US", "lat": 29.0, "lng": -95.3, "mtpa": 15, "role": "Export"},
    {"name": "Cameron LNG",    "country": "US", "lat": 29.8, "lng": -93.3, "mtpa": 13, "role": "Export"},
    {"name": "Yamal LNG",      "country": "RU", "lat": 71.3, "lng":  72.1, "mtpa": 18, "role": "Export"},
    {"name": "Sakhalin-2",     "country": "RU", "lat": 46.6, "lng": 142.7, "mtpa": 11, "role": "Export"},
    {"name": "Gorgon",         "country": "AU", "lat":-20.7, "lng": 115.5, "mtpa": 16, "role": "Export"},
    {"name": "Ichthys",        "country": "AU", "lat":-12.5, "lng": 130.8, "mtpa":  9, "role": "Export"},
    {"name": "Wheatstone",     "country": "AU", "lat":-21.7, "lng": 115.0, "mtpa":  9, "role": "Export"},
    {"name": "Bintulu MLNG",   "country": "MY", "lat":  3.2, "lng": 113.0, "mtpa": 30, "role": "Export"},
    {"name": "Bonny LNG",      "country": "NG", "lat":  4.4, "lng":   7.2, "mtpa": 22, "role": "Export"},
]

# 5-year renewable-share forecast (OLS regression on 10y of OWID data).
ENERGY_FORECAST = {
    "world": {
        "current_year":   2025,
        "current_share":  0.338,
        "horizon_year":   2030,
        "central":        0.381,
        "band_low":       0.376,
        "band_high":      0.386,
        "slope_pp_year":  1.05,
        "residual_std_pp": 0.52,
    },
    "country_top_growth": [
        {"country": "Netherlands",   "now": 0.512, "in_5y": 0.753, "slope_pp_year":  4.57},
        {"country": "Germany",       "now": 0.591, "in_5y": 0.742, "slope_pp_year":  3.15},
        {"country": "United Kingdom","now": 0.520, "in_5y": 0.668, "slope_pp_year":  2.85},
        {"country": "Australia",     "now": 0.386, "in_5y": 0.519, "slope_pp_year":  2.65},
        {"country": "Spain",         "now": 0.559, "in_5y": 0.667, "slope_pp_year":  2.32},
        {"country": "Poland",        "now": 0.315, "in_5y": 0.394, "slope_pp_year":  1.97},
        {"country": "Sweden",        "now": 0.712, "in_5y": 0.785, "slope_pp_year":  1.42},
        {"country": "Turkey",        "now": 0.433, "in_5y": 0.524, "slope_pp_year":  1.42},
        {"country": "China",         "now": 0.370, "in_5y": 0.402, "slope_pp_year":  1.16},
        {"country": "Brazil",        "now": 0.866, "in_5y": 0.940, "slope_pp_year":  1.14},
        {"country": "United States", "now": 0.256, "in_5y": 0.310, "slope_pp_year":  1.12},
        {"country": "Italy",         "now": 0.488, "in_5y": 0.521, "slope_pp_year":  1.08},
        {"country": "Japan",         "now": 0.236, "in_5y": 0.288, "slope_pp_year":  1.00},
    ],
    "method": "OLS linreg on last 10y of renewables_share_elec, 5y horizon, ±1σ band, clamped [0,1].",
}

# Snapshot of energy futures, ETFs, equities (Stooq end-of-day).
ENERGY_DERIVATIVES = {
    "futures": [
        {"symbol": "CL", "name": "WTI Crude Oil",            "category": "Crude oil",   "price": 101.94, "change_pct": -3.04},
        {"symbol": "NG", "name": "Natural Gas (Henry Hub)",  "category": "Natural gas", "price":   2.78, "change_pct": +0.91},
        {"symbol": "HO", "name": "Heating Oil",              "category": "Distillates", "price":   3.95, "change_pct": -3.61},
        {"symbol": "RB", "name": "RBOB Gasoline",            "category": "Distillates", "price":   3.60, "change_pct": -0.99},
    ],
    "etfs": [
        {"symbol": "XLE",  "name": "Energy Select Sector SPDR",     "category": "Broad energy",      "price":  58.85, "change_pct": -0.36},
        {"symbol": "XOP",  "name": "S&P Oil & Gas E&P",             "category": "Oil E&P",           "price": 176.67, "change_pct": -0.08},
        {"symbol": "USO",  "name": "United States Oil Fund",        "category": "Crude oil",         "price": 142.80, "change_pct": -0.51},
        {"symbol": "UNG",  "name": "United States Natural Gas Fund","category": "Natural gas",       "price":  10.71, "change_pct": +0.19},
        {"symbol": "TAN",  "name": "Invesco Solar ETF",             "category": "Solar",             "price":  59.27, "change_pct": +1.58},
        {"symbol": "ICLN", "name": "iShares Global Clean Energy",   "category": "Clean energy",      "price":  20.95, "change_pct": +0.62},
        {"symbol": "URA",  "name": "Global X Uranium",              "category": "Uranium",           "price":  55.84, "change_pct": -0.27},
        {"symbol": "NLR",  "name": "VanEck Uranium+Nuclear Energy", "category": "Nuclear",           "price": 144.00, "change_pct": -0.41},
        {"symbol": "LIT",  "name": "Global X Lithium & Battery",    "category": "Lithium / battery", "price":  88.72, "change_pct": +0.75},
        {"symbol": "FAN",  "name": "First Trust Global Wind Energy","category": "Wind",              "price":  26.87, "change_pct": -0.04},
    ],
    "equities": [
        {"symbol": "XOM",  "name": "ExxonMobil",        "category": "Oil major",     "price": 152.75, "change_pct": +0.09},
        {"symbol": "CVX",  "name": "Chevron",           "category": "Oil major",     "price": 190.63, "change_pct": -0.35},
        {"symbol": "SHEL", "name": "Shell",             "category": "Oil major",     "price":  88.98, "change_pct": -1.35},
        {"symbol": "BP",   "name": "BP",                "category": "Oil major",     "price":  46.41, "change_pct": -1.42},
        {"symbol": "TTE",  "name": "TotalEnergies",     "category": "Oil major",     "price":  92.78, "change_pct": +0.37},
        {"symbol": "COP",  "name": "ConocoPhillips",    "category": "Oil E&P",       "price": 123.19, "change_pct": -1.45},
        {"symbol": "EOG",  "name": "EOG Resources",     "category": "Oil E&P",       "price": 138.95, "change_pct": -0.67},
        {"symbol": "OXY",  "name": "Occidental",        "category": "Oil E&P",       "price":  58.71, "change_pct": -2.22},
        {"symbol": "FANG", "name": "Diamondback",       "category": "Oil E&P",       "price": 207.65, "change_pct": +1.51},
        {"symbol": "EQT",  "name": "EQT Corporation",   "category": "Natural gas",   "price":  58.66, "change_pct": -2.23},
        {"symbol": "SLB",  "name": "Schlumberger",      "category": "Oil services",  "price":  56.92, "change_pct": +0.87},
        {"symbol": "HAL",  "name": "Halliburton",       "category": "Oil services",  "price":  41.66, "change_pct": -1.88},
        {"symbol": "NEE",  "name": "NextEra Energy",    "category": "Util / renew",  "price":  96.95, "change_pct": -1.07},
        {"symbol": "DUK",  "name": "Duke Energy",       "category": "Utility",       "price": 128.60, "change_pct": -0.65},
        {"symbol": "SO",   "name": "Southern Company",  "category": "Utility",       "price":  96.71, "change_pct": +0.14},
        {"symbol": "ENPH", "name": "Enphase Energy",    "category": "Solar",         "price":  33.85, "change_pct": +1.85},
        {"symbol": "FSLR", "name": "First Solar",       "category": "Solar",         "price": 211.71, "change_pct": +5.81},
        {"symbol": "CEG",  "name": "Constellation Energy","category": "Nuclear",     "price": 307.81, "change_pct": -1.50},
        {"symbol": "VST",  "name": "Vistra",            "category": "Power gen",     "price": 155.28, "change_pct": -1.91},
    ],
    "source": "Stooq end-of-day snapshot",
}


BTC_ETF_FLOWS = [
    {"ticker": "IBIT", "name": "iShares Bitcoin Trust", "issuer": "BlackRock", "aum_b": 58.4, "flow_24h_m": 142.6, "flow_7d_m": 425.8, "expense_pct": 0.25},
    {"ticker": "FBTC", "name": "Fidelity Wise Origin Bitcoin", "issuer": "Fidelity", "aum_b": 19.7, "flow_24h_m": 38.2, "flow_7d_m": 156.3, "expense_pct": 0.25},
    {"ticker": "ARKB", "name": "ARK 21Shares Bitcoin", "issuer": "ARK/21Shares", "aum_b": 4.2, "flow_24h_m": 8.4, "flow_7d_m": 32.1, "expense_pct": 0.21},
    {"ticker": "BITB", "name": "Bitwise Bitcoin", "issuer": "Bitwise", "aum_b": 3.8, "flow_24h_m": 6.7, "flow_7d_m": 28.4, "expense_pct": 0.20},
    {"ticker": "GBTC", "name": "Grayscale Bitcoin Trust", "issuer": "Grayscale", "aum_b": 17.2, "flow_24h_m": -42.1, "flow_7d_m": -187.3, "expense_pct": 1.50},
    {"ticker": "HODL", "name": "VanEck Bitcoin", "issuer": "VanEck", "aum_b": 1.4, "flow_24h_m": 2.3, "flow_7d_m": 11.2, "expense_pct": 0.20},
    {"ticker": "BRRR", "name": "Valkyrie Bitcoin", "issuer": "Valkyrie", "aum_b": 0.8, "flow_24h_m": 1.1, "flow_7d_m": 4.3, "expense_pct": 0.25},
    {"ticker": "EZBC", "name": "Franklin Bitcoin", "issuer": "Franklin Templeton", "aum_b": 0.6, "flow_24h_m": 0.9, "flow_7d_m": 3.8, "expense_pct": 0.19},
    {"ticker": "BTCO", "name": "Invesco Galaxy Bitcoin", "issuer": "Invesco/Galaxy", "aum_b": 0.7, "flow_24h_m": 1.2, "flow_7d_m": 4.7, "expense_pct": 0.25},
    {"ticker": "ETHA", "name": "iShares Ethereum Trust", "issuer": "BlackRock", "aum_b": 8.4, "flow_24h_m": 18.6, "flow_7d_m": 67.2, "expense_pct": 0.25},
    {"ticker": "FETH", "name": "Fidelity Ethereum", "issuer": "Fidelity", "aum_b": 1.9, "flow_24h_m": 3.2, "flow_7d_m": 12.4, "expense_pct": 0.25},
]

STABLECOINS = [
    {"symbol": "USDT", "name": "Tether", "issuer": "Tether Ltd", "marketcap_b": 142.6, "share_pct": 67.2, "chain": "Multi", "backing": "Cash + T-bills + commercial"},
    {"symbol": "USDC", "name": "USD Coin", "issuer": "Circle", "marketcap_b": 47.8, "share_pct": 22.5, "chain": "Multi", "backing": "Cash + short T-bills (audited)"},
    {"symbol": "DAI", "name": "MakerDAO Dai", "issuer": "MakerDAO", "marketcap_b": 5.4, "share_pct": 2.5, "chain": "Ethereum", "backing": "Crypto-collateralized + RWA"},
    {"symbol": "USDe", "name": "Ethena USDe", "issuer": "Ethena Labs", "marketcap_b": 4.8, "share_pct": 2.3, "chain": "Multi", "backing": "Delta-neutral synthetic"},
    {"symbol": "FDUSD", "name": "First Digital USD", "issuer": "First Digital", "marketcap_b": 2.1, "share_pct": 1.0, "chain": "Multi", "backing": "Cash + T-bills"},
    {"symbol": "PYUSD", "name": "PayPal USD", "issuer": "Paxos", "marketcap_b": 1.8, "share_pct": 0.8, "chain": "Multi", "backing": "Cash + T-bills"},
    {"symbol": "TUSD", "name": "TrueUSD", "issuer": "Archblock", "marketcap_b": 0.5, "share_pct": 0.2, "chain": "Multi", "backing": "Cash equivalents"},
    {"symbol": "USDP", "name": "Pax Dollar", "issuer": "Paxos", "marketcap_b": 0.3, "share_pct": 0.1, "chain": "Ethereum", "backing": "Cash + T-bills"},
    {"symbol": "GUSD", "name": "Gemini Dollar", "issuer": "Gemini", "marketcap_b": 0.1, "share_pct": 0.05, "chain": "Ethereum", "backing": "Cash + T-bills"},
    {"symbol": "EURS", "name": "STASIS EURS", "issuer": "STASIS", "marketcap_b": 0.1, "share_pct": 0.05, "chain": "Multi", "backing": "EUR cash"},
]


# ═══════════════ GOVERNMENT SPENDING / USASPENDING ═══════════════
GOV_SPENDING = [
    {"recipient": "Lockheed Martin", "amount_m": 4280, "agency": "DOD", "date": "2026-03-28", "purpose": "F-35 Lot 19 production", "country": "US"},
    {"recipient": "RTX (Raytheon)", "amount_m": 2150, "agency": "DOD", "date": "2026-03-25", "purpose": "Patriot/PAC-3 missile production for allies", "country": "US"},
    {"recipient": "Northrop Grumman", "amount_m": 1820, "agency": "DOD", "date": "2026-03-22", "purpose": "B-21 Raider production milestone", "country": "US"},
    {"recipient": "General Dynamics", "amount_m": 1640, "agency": "DOD", "date": "2026-03-19", "purpose": "Virginia-class submarine block VI", "country": "US"},
    {"recipient": "Boeing Defense", "amount_m": 1280, "agency": "DOD", "date": "2026-03-15", "purpose": "KC-46 tanker fleet sustainment", "country": "US"},
    {"recipient": "L3Harris", "amount_m": 760, "agency": "DOD", "date": "2026-03-12", "purpose": "Tactical comms, EW systems", "country": "US"},
    {"recipient": "Anduril Industries", "amount_m": 645, "agency": "DOD", "date": "2026-03-10", "purpose": "CCA (Collaborative Combat Aircraft) Lot 1", "country": "US"},
    {"recipient": "Palantir Technologies", "amount_m": 480, "agency": "DOD", "date": "2026-03-08", "purpose": "Maven Smart System expansion", "country": "US"},
    {"recipient": "SpaceX", "amount_m": 1150, "agency": "USSF/NASA", "date": "2026-03-05", "purpose": "NSSL launches + Starship lunar", "country": "US"},
    {"recipient": "Booz Allen Hamilton", "amount_m": 320, "agency": "DOD/Intel", "date": "2026-03-03", "purpose": "Cyber operations support", "country": "US"},
    {"recipient": "Microsoft", "amount_m": 410, "agency": "DOD", "date": "2026-02-28", "purpose": "Azure Government cloud expansion", "country": "US"},
    {"recipient": "Oracle", "amount_m": 280, "agency": "DOD", "date": "2026-02-25", "purpose": "JWCC compute allocation", "country": "US"},
    {"recipient": "Amazon Web Services", "amount_m": 365, "agency": "Intel", "date": "2026-02-22", "purpose": "C2S top-secret cloud", "country": "US"},
    {"recipient": "BAE Systems", "amount_m": 540, "agency": "DOD", "date": "2026-02-20", "purpose": "M88 Hercules recovery vehicle", "country": "US"},
    {"recipient": "Huntington Ingalls", "amount_m": 920, "agency": "USN", "date": "2026-02-18", "purpose": "DDG-51 Flight III destroyer", "country": "US"},
]


# ═══════════════ LAYOFFS TRACKER ═══════════════
LAYOFFS_TRACKER = [
    {"company": "Microsoft", "country": "US", "count": 6800, "date": "2026-04-02", "sector": "Tech", "reason": "AI restructuring; Azure team consolidation"},
    {"company": "Meta", "country": "US", "count": 4200, "date": "2026-03-28", "sector": "Tech", "reason": "Reality Labs efficiency push"},
    {"company": "Google", "country": "US", "count": 3500, "date": "2026-03-25", "sector": "Tech", "reason": "Hardware + Pixel team layoffs"},
    {"company": "Salesforce", "country": "US", "count": 2400, "date": "2026-03-20", "sector": "Tech", "reason": "Sales + customer success cuts"},
    {"company": "Amazon", "country": "US", "count": 5800, "date": "2026-03-18", "sector": "Tech", "reason": "AWS restructure; books team"},
    {"company": "Intel", "country": "US", "count": 3200, "date": "2026-03-15", "sector": "Semi", "reason": "Foundry losses; manufacturing consolidation"},
    {"company": "Tesla", "country": "US", "count": 2800, "date": "2026-03-12", "sector": "Auto", "reason": "Q1 deliveries miss; cost cuts"},
    {"company": "Ford", "country": "US", "count": 1900, "date": "2026-03-10", "sector": "Auto", "reason": "EV slowdown; F-150 Lightning team"},
    {"company": "Citigroup", "country": "US", "count": 2200, "date": "2026-03-08", "sector": "Finance", "reason": "Org simplification continues"},
    {"company": "Goldman Sachs", "country": "US", "count": 1800, "date": "2026-03-05", "sector": "Finance", "reason": "Performance reviews + market downturn"},
    {"company": "Boeing", "country": "US", "count": 4100, "date": "2026-03-01", "sector": "Aero", "reason": "Defense + commercial cuts post-strike"},
    {"company": "SAP", "country": "DE", "count": 8000, "date": "2026-02-25", "sector": "Tech", "reason": "AI-driven roles transformation"},
    {"company": "Spotify", "country": "SE", "count": 1500, "date": "2026-02-22", "sector": "Tech", "reason": "R&D consolidation"},
    {"company": "Block (Square)", "country": "US", "count": 1200, "date": "2026-02-18", "sector": "Fintech", "reason": "Cash App + Square reorg"},
    {"company": "Stripe", "country": "US", "count": 980, "date": "2026-02-15", "sector": "Fintech", "reason": "Cost discipline ahead of IPO"},
    {"company": "Workday", "country": "US", "count": 1100, "date": "2026-02-12", "sector": "Tech", "reason": "Re-org around AI agents"},
]


# ═══════════════ ISRAEL SIRENS / RED ALERT ═══════════════
ISRAEL_SIRENS = [
    {"location": "Tel Aviv", "lat": 32.085, "lng": 34.781, "time": "2026-04-08T22:14:00Z", "threat": "Rocket fire", "origin": "Gaza"},
    {"location": "Sderot", "lat": 31.525, "lng": 34.595, "time": "2026-04-08T20:42:00Z", "threat": "Mortar fire", "origin": "Gaza"},
    {"location": "Ashkelon", "lat": 31.668, "lng": 34.572, "time": "2026-04-08T18:33:00Z", "threat": "Rocket fire", "origin": "Gaza"},
    {"location": "Kiryat Shmona", "lat": 33.207, "lng": 35.572, "time": "2026-04-07T15:21:00Z", "threat": "Anti-tank fire", "origin": "Lebanon"},
    {"location": "Metula", "lat": 33.279, "lng": 35.580, "time": "2026-04-07T11:08:00Z", "threat": "UAV intrusion", "origin": "Lebanon"},
    {"location": "Haifa", "lat": 32.794, "lng": 34.989, "time": "2026-04-06T19:45:00Z", "threat": "Hostile UAV", "origin": "Lebanon"},
    {"location": "Eilat", "lat": 29.557, "lng": 34.952, "time": "2026-04-05T08:30:00Z", "threat": "Cruise missile", "origin": "Yemen"},
    {"location": "Be'er Sheva", "lat": 31.252, "lng": 34.792, "time": "2026-04-04T21:12:00Z", "threat": "Ballistic missile", "origin": "Yemen"},
    {"location": "Netivot", "lat": 31.422, "lng": 34.589, "time": "2026-04-03T16:55:00Z", "threat": "Rocket fire", "origin": "Gaza"},
    {"location": "Nahariya", "lat": 33.006, "lng": 35.094, "time": "2026-04-02T13:20:00Z", "threat": "Anti-tank fire", "origin": "Lebanon"},
]


# ═══════════════ TELEGRAM INTEL CHANNELS ═══════════════
TELEGRAM_INTEL = [
    {"channel": "@war_monitor", "subscribers_k": 480, "category": "Conflict", "language": "EN/RU", "last_update": "2026-04-08T22:00:00Z", "summary": "Russian glide bomb strikes on Kharkiv overnight; 14 dead"},
    {"channel": "@bellingcat", "subscribers_k": 320, "category": "OSINT", "language": "EN", "last_update": "2026-04-08T19:30:00Z", "summary": "Geolocated Russian S-400 system near Crimea bridge"},
    {"channel": "@conflicts_news", "subscribers_k": 760, "category": "Conflict", "language": "EN", "last_update": "2026-04-08T21:15:00Z", "summary": "IDF strikes on Hezbollah positions in southern Lebanon"},
    {"channel": "@military_news_x", "subscribers_k": 540, "category": "Military", "language": "EN", "last_update": "2026-04-08T20:00:00Z", "summary": "PLA Navy Type 003 carrier sea trials extending"},
    {"channel": "@nuclear_radio", "subscribers_k": 220, "category": "Nuclear", "language": "EN", "last_update": "2026-04-08T17:45:00Z", "summary": "IAEA Zaporizhzhia inspection report flags safety regress"},
    {"channel": "@osint_drones", "subscribers_k": 180, "category": "OSINT/UAV", "language": "EN", "last_update": "2026-04-08T18:30:00Z", "summary": "New Iranian Shahed-238 jet variant photographed"},
    {"channel": "@kyiv_independent_news", "subscribers_k": 410, "category": "Ukraine", "language": "EN", "last_update": "2026-04-08T22:30:00Z", "summary": "Frontline situation report Donetsk axis"},
    {"channel": "@anti_corruption_kr", "subscribers_k": 95, "category": "DPRK", "language": "KO/EN", "last_update": "2026-04-07T16:20:00Z", "summary": "Pyongyang grain shipments tracked via satellite"},
    {"channel": "@gaza_now", "subscribers_k": 680, "category": "Gaza", "language": "AR/EN", "last_update": "2026-04-08T21:50:00Z", "summary": "Strikes on Khan Yunis; UNRWA aid convoy blocked"},
    {"channel": "@maritime_intel", "subscribers_k": 240, "category": "Maritime", "language": "EN", "last_update": "2026-04-08T15:30:00Z", "summary": "Houthi anti-ship missile attempt on tanker MV Polaris"},
    {"channel": "@cyber_threat_news", "subscribers_k": 165, "category": "Cyber", "language": "EN", "last_update": "2026-04-08T14:00:00Z", "summary": "Volt Typhoon TTPs targeting US water utilities updated"},
    {"channel": "@africa_intel", "subscribers_k": 88, "category": "Africa", "language": "EN/FR", "last_update": "2026-04-08T12:30:00Z", "summary": "Wagner/Africa Corps activity near Bamako"},
    {"channel": "@taiwan_strait_watch", "subscribers_k": 130, "category": "Asia", "language": "EN", "last_update": "2026-04-08T11:00:00Z", "summary": "PLA aircraft sortie count: 38 in 24h, 12 crossing median line"},
    {"channel": "@arctic_intel", "subscribers_k": 42, "category": "Arctic", "language": "EN", "last_update": "2026-04-07T20:15:00Z", "summary": "Russian Northern Fleet exercises in Barents Sea"},
    {"channel": "@dronewatch_il", "subscribers_k": 76, "category": "Israel", "language": "EN", "last_update": "2026-04-08T22:10:00Z", "summary": "Iron Dome interceptions over central Israel — 4 launches"},
]


# ═══════════════ TECH READINESS LEVEL TRACKER ═══════════════
TECH_READINESS = [
    {"tech": "Quantum computing (logical qubits)", "trl": 5, "leader": "Google/IBM/Quantinuum", "status": "Crossed error-correction threshold (2024)", "year_field": 2030},
    {"tech": "Fusion (commercial Q>1)", "trl": 4, "leader": "Commonwealth/Helion/TAE", "status": "Net-energy demonstrated (NIF 2022); commercial pilots designed", "year_field": 2032},
    {"tech": "AGI (Level-5 reasoning)", "trl": 6, "leader": "OpenAI/Anthropic/Google DeepMind", "status": "Frontier models passing professional exams; persistent gaps in long-horizon planning", "year_field": 2028},
    {"tech": "Self-driving (L4 unrestricted)", "trl": 7, "leader": "Waymo/Cruise/Tesla", "status": "Geofenced L4 in 5 US cities; weather/night limits remain", "year_field": 2027},
    {"tech": "mRNA vaccines (cancer)", "trl": 7, "leader": "Moderna/BioNTech", "status": "Phase 3 melanoma + colorectal trials", "year_field": 2027},
    {"tech": "Direct air capture", "trl": 6, "leader": "Climeworks/Heirloom/1PointFive", "status": "Mammoth (36kt/y) operational; cost still ~$600/t", "year_field": 2030},
    {"tech": "SMRs (commercial)", "trl": 6, "leader": "NuScale/X-Energy/TerraPower", "status": "Several NRC design certifications; first ops site by 2029", "year_field": 2029},
    {"tech": "Hypersonics (mass production)", "trl": 7, "leader": "RU/CN deployed; US LRHW initial fielding", "status": "Russia Avangard/Kinzhal operational; US fielding delays", "year_field": 2026},
    {"tech": "Brain-computer interface (clinical)", "trl": 6, "leader": "Neuralink/Synchron/Blackrock", "status": "First in-human implants 2024; ALS communication trials", "year_field": 2028},
    {"tech": "Solid-state batteries (auto)", "trl": 6, "leader": "Toyota/QuantumScape/SES", "status": "Pilot lines 2026; mass production targeted 2027-2028", "year_field": 2028},
    {"tech": "Lab-grown meat (price parity)", "trl": 5, "leader": "UPSIDE/Eat Just/Mosa Meat", "status": "Singapore + US approvals; cost still 100×+ conventional", "year_field": 2032},
    {"tech": "Asteroid mining (return sample)", "trl": 4, "leader": "AstroForge/TransAstra", "status": "OSIRIS-REx returned Bennu sample 2023; commercial yet to demonstrate", "year_field": 2035},
]


# ═══════════════ STRATEGIC POSTURE THEATERS ═══════════════
STRATEGIC_POSTURE = [
    {"theater": "Indo-Pacific", "lat": 20, "lng": 140, "us_forces": 375000, "rival": "PRC", "rival_forces": 2200000, "alert": "ELEVATED", "summary": "Fleet rotation Carrier Strike Groups 11 + 9 + 5; Marine Littoral Regiment forward; PLA Navy at 360 ships"},
    {"theater": "Europe (NATO)", "lat": 52, "lng": 15, "us_forces": 100000, "rival": "RUS", "rival_forces": 550000, "alert": "HIGH", "summary": "V Corps fwd HQ Poznan; 2 ABCTs rotational; 3 ROK battle group; UK Op INTERFLEX training Ukrainians"},
    {"theater": "Middle East (CENTCOM)", "lat": 28, "lng": 50, "us_forces": 45000, "rival": "IRN", "rival_forces": 580000, "alert": "HIGH", "summary": "CSG Truman in 5th Fleet AOR; Patriot/THAAD in Kuwait/UAE/SA; F-22/F-35 surge"},
    {"theater": "Africa (AFRICOM)", "lat": 5, "lng": 25, "us_forces": 6500, "rival": "Wagner/Africa Corps", "rival_forces": 8000, "alert": "MEDIUM", "summary": "Camp Lemonnier hub; Sahel withdrawal; ISWAP/AQIM monitoring"},
    {"theater": "Latin America (SOUTHCOM)", "lat": 5, "lng": -75, "us_forces": 2000, "rival": "Cartels/Maduro", "rival_forces": 0, "alert": "MEDIUM", "summary": "Counter-narcotics + Venezuela posture; HMS Lancaster in Caribbean"},
    {"theater": "Arctic", "lat": 80, "lng": 0, "us_forces": 5000, "rival": "RUS", "rival_forces": 27000, "alert": "MEDIUM", "summary": "Eielson F-35s; submarine ICEX exercises; Russian Northern Fleet posture"},
]


# ═══════════════ LIVE INTELLIGENCE FEEDS (GDELT-style topical) ═══════════════
LIVE_INTELLIGENCE_FEEDS = {
    "military": [
        {"headline": "PLA Navy Type 076 amphib begins sea trials at Jiangnan shipyard", "source": "Janes", "time": "2026-04-08T20:30:00Z", "country": "CN"},
        {"headline": "US 7th Fleet conducts FONOP in South China Sea near Mischief Reef", "source": "USNI", "time": "2026-04-08T18:00:00Z", "country": "CN"},
        {"headline": "Russian Iskander launches confirmed near Belgorod", "source": "GUR", "time": "2026-04-08T16:00:00Z", "country": "RU"},
        {"headline": "IDF announces Lebanese front escalation; northern reservists called up", "source": "IDF", "time": "2026-04-08T15:30:00Z", "country": "IL"},
        {"headline": "North Korean ICBM test from Sino-ri site reported", "source": "NIS", "time": "2026-04-07T22:00:00Z", "country": "KP"},
        {"headline": "UK Type 26 frigate HMS Glasgow handover delayed Q3 2026", "source": "RN", "time": "2026-04-07T15:00:00Z", "country": "GB"},
    ],
    "cyber": [
        {"headline": "CISA warns of active Volt Typhoon water utility intrusions", "source": "CISA", "time": "2026-04-08T19:00:00Z", "country": "US"},
        {"headline": "Microsoft patches zero-day actively exploited by APT29", "source": "MSRC", "time": "2026-04-08T16:30:00Z", "country": "US"},
        {"headline": "EU NIS2 directive enforcement begins for medium-size critical entities", "source": "ENISA", "time": "2026-04-08T12:00:00Z", "country": "EU"},
        {"headline": "Lazarus Group cryptocurrency theft tops $1.4B in 2026 YTD", "source": "Chainalysis", "time": "2026-04-07T20:00:00Z", "country": "KP"},
        {"headline": "Brazil PIX system DDoS attempt; central bank confirms degraded service", "source": "BCB", "time": "2026-04-07T14:00:00Z", "country": "BR"},
    ],
    "nuclear": [
        {"headline": "IAEA flags safety regress at Zaporizhzhia NPP unit 6", "source": "IAEA", "time": "2026-04-08T18:30:00Z", "country": "UA"},
        {"headline": "US-UK AUKUS pillar 1 SSN-AUKUS first cut steel 2026", "source": "DOD", "time": "2026-04-08T11:00:00Z", "country": "AU"},
        {"headline": "Iran enriches uranium to 84% at Fordow per IAEA", "source": "IAEA", "time": "2026-04-07T19:00:00Z", "country": "IR"},
        {"headline": "China expanding Lop Nur missile silo field; commercial imagery", "source": "FAS", "time": "2026-04-07T13:00:00Z", "country": "CN"},
        {"headline": "Russia reaffirms doctrine on lower nuclear use threshold", "source": "TASS", "time": "2026-04-06T17:00:00Z", "country": "RU"},
    ],
    "sanctions": [
        {"headline": "OFAC adds 84 entities to Russia secondary sanctions list", "source": "Treasury", "time": "2026-04-08T17:00:00Z", "country": "US"},
        {"headline": "EU 14th sanctions package targets shadow fleet vessels", "source": "EU Council", "time": "2026-04-08T13:00:00Z", "country": "EU"},
        {"headline": "UK designates 4 Iranian banks under Iran sanctions", "source": "FCDO", "time": "2026-04-07T11:00:00Z", "country": "GB"},
        {"headline": "OFAC settles with crypto exchange for $24M sanctions violations", "source": "Treasury", "time": "2026-04-06T15:00:00Z", "country": "US"},
        {"headline": "Switzerland adopts EU sanctions on Russia's shadow fleet", "source": "SECO", "time": "2026-04-05T10:00:00Z", "country": "CH"},
    ],
}


# ═══════════════ POPULATION EXPOSURE (at-risk metrics) ═══════════════
POPULATION_EXPOSURE = [
    {"region": "Bangladesh delta (sea level rise)", "country": "BD", "lat": 23.685, "lng": 90.356, "population_m": 32, "hazard": "Coastal flood", "horizon_y": 30},
    {"region": "Jakarta (subsidence + flood)", "country": "ID", "lat": -6.21, "lng": 106.85, "population_m": 11, "hazard": "Subsidence + sea level", "horizon_y": 20},
    {"region": "Sahel (food insecurity)", "country": "Multi", "lat": 14, "lng": 0, "population_m": 50, "hazard": "Drought + conflict", "horizon_y": 5},
    {"region": "Tokyo (megaquake risk)", "country": "JP", "lat": 35.68, "lng": 139.69, "population_m": 37, "hazard": "M9 Nankai trough", "horizon_y": 30},
    {"region": "Cascadia subduction zone", "country": "US", "lat": 45, "lng": -123, "population_m": 8, "hazard": "M9 earthquake/tsunami", "horizon_y": 50},
    {"region": "Manila (typhoon)", "country": "PH", "lat": 14.6, "lng": 121.0, "population_m": 14, "hazard": "Typhoon", "horizon_y": 5},
    {"region": "Karachi (heat dome)", "country": "PK", "lat": 24.86, "lng": 67.0, "population_m": 17, "hazard": "Wet-bulb extreme heat", "horizon_y": 10},
    {"region": "Kinshasa (food + water)", "country": "CD", "lat": -4.32, "lng": 15.32, "population_m": 17, "hazard": "Urban food/water crisis", "horizon_y": 10},
    {"region": "Lagos (sea level + flood)", "country": "NG", "lat": 6.5, "lng": 3.4, "population_m": 22, "hazard": "Coastal flood", "horizon_y": 30},
    {"region": "Cairo (heat + Nile)", "country": "EG", "lat": 30.04, "lng": 31.24, "population_m": 21, "hazard": "Water stress + heat", "horizon_y": 20},
]


# ═══════════════ GLOBAL MARKET INDICES ═══════════════
MARKET_INDICES = [
    {"symbol": "SPX",     "name": "S&P 500",          "region": "US",      "country": "US", "value": 5723.50, "ch_pct":  0.35, "ytd_pct":  20.1},
    {"symbol": "IXIC",    "name": "NASDAQ Composite", "region": "US",      "country": "US", "value": 18113.0, "ch_pct":  0.42, "ytd_pct":  20.5},
    {"symbol": "DJI",     "name": "Dow Jones",        "region": "US",      "country": "US", "value": 42060.0, "ch_pct":  0.18, "ytd_pct":  11.6},
    {"symbol": "RUT",     "name": "Russell 2000",     "region": "US",      "country": "US", "value":  2230.0, "ch_pct": -0.12, "ytd_pct":  10.5},
    {"symbol": "VIX",     "name": "VIX Volatility",   "region": "US",      "country": "US", "value":    16.4, "ch_pct": -2.10, "ytd_pct":  31.0},
    {"symbol": "FTSE",    "name": "FTSE 100",         "region": "Europe",  "country": "GB", "value":  8275.0, "ch_pct":  0.20, "ytd_pct":   7.0},
    {"symbol": "DAX",     "name": "DAX 40",           "region": "Europe",  "country": "DE", "value": 19120.0, "ch_pct":  0.15, "ytd_pct":  14.0},
    {"symbol": "CAC",     "name": "CAC 40",           "region": "Europe",  "country": "FR", "value":  7600.0, "ch_pct":  0.10, "ytd_pct":   1.5},
    {"symbol": "IBEX",    "name": "IBEX 35",          "region": "Europe",  "country": "ES", "value": 11800.0, "ch_pct":  0.18, "ytd_pct":  16.0},
    {"symbol": "FTSEMIB", "name": "FTSE MIB",         "region": "Europe",  "country": "IT", "value": 33950.0, "ch_pct":  0.25, "ytd_pct":  11.7},
    {"symbol": "AEX",     "name": "AEX 25",           "region": "Europe",  "country": "NL", "value":   895.0, "ch_pct":  0.30, "ytd_pct":  13.4},
    {"symbol": "SMI",     "name": "Swiss Market",     "region": "Europe",  "country": "CH", "value": 12100.0, "ch_pct":  0.10, "ytd_pct":   8.7},
    {"symbol": "OMXS30",  "name": "OMX Stockholm 30", "region": "Europe",  "country": "SE", "value":  2580.0, "ch_pct":  0.40, "ytd_pct":   8.0},
    {"symbol": "WIG20",   "name": "WIG20",            "region": "Europe",  "country": "PL", "value":  2500.0, "ch_pct":  0.15, "ytd_pct":   7.4},
    {"symbol": "MOEX",    "name": "MOEX Russia",      "region": "Europe",  "country": "RU", "value":  2750.0, "ch_pct": -0.50, "ytd_pct":  -8.0},
    {"symbol": "N225",    "name": "Nikkei 225",       "region": "Asia",    "country": "JP", "value": 38000.0, "ch_pct":  0.50, "ytd_pct":  13.5},
    {"symbol": "TOPIX",   "name": "TOPIX",            "region": "Asia",    "country": "JP", "value":  2660.0, "ch_pct":  0.45, "ytd_pct":  12.6},
    {"symbol": "HSI",     "name": "Hang Seng",        "region": "Asia",    "country": "HK", "value": 21500.0, "ch_pct":  0.65, "ytd_pct":  26.0},
    {"symbol": "SHCOMP",  "name": "Shanghai SSE",     "region": "Asia",    "country": "CN", "value":  3270.0, "ch_pct":  0.30, "ytd_pct":   9.8},
    {"symbol": "SZCOMP",  "name": "Shenzhen SZSE",    "region": "Asia",    "country": "CN", "value": 10100.0, "ch_pct":  0.40, "ytd_pct":  15.2},
    {"symbol": "KOSPI",   "name": "KOSPI",            "region": "Asia",    "country": "KR", "value":  2680.0, "ch_pct":  0.15, "ytd_pct":   1.0},
    {"symbol": "TWII",    "name": "Taiwan Weighted",  "region": "Asia",    "country": "TW", "value": 22850.0, "ch_pct":  0.55, "ytd_pct":  27.5},
    {"symbol": "SENSEX",  "name": "BSE Sensex",       "region": "Asia",    "country": "IN", "value": 82000.0, "ch_pct":  0.20, "ytd_pct":  13.5},
    {"symbol": "NIFTY",   "name": "Nifty 50",         "region": "Asia",    "country": "IN", "value": 25100.0, "ch_pct":  0.25, "ytd_pct":  15.5},
    {"symbol": "STI",     "name": "Straits Times",    "region": "Asia",    "country": "SG", "value":  3580.0, "ch_pct":  0.10, "ytd_pct":  10.5},
    {"symbol": "AXJO",    "name": "ASX 200",          "region": "Oceania", "country": "AU", "value":  8200.0, "ch_pct":  0.30, "ytd_pct":   8.0},
    {"symbol": "TSX",     "name": "S&P/TSX",          "region": "Americas","country": "CA", "value": 23800.0, "ch_pct":  0.20, "ytd_pct":  13.5},
    {"symbol": "BOVESPA", "name": "Bovespa",          "region": "Americas","country": "BR", "value":131500.0, "ch_pct": -0.10, "ytd_pct":  -2.0},
    {"symbol": "MEXBOL",  "name": "IPC Mexico",       "region": "Americas","country": "MX", "value": 51500.0, "ch_pct":  0.05, "ytd_pct":  -9.5},
    {"symbol": "TA35",    "name": "TA-35 Tel Aviv",   "region": "MENA",    "country": "IL", "value":  2100.0, "ch_pct":  0.20, "ytd_pct":  10.0},
    {"symbol": "TASI",    "name": "Tadawul",          "region": "MENA",    "country": "SA", "value": 12100.0, "ch_pct": -0.20, "ytd_pct":   1.0},
    {"symbol": "JSE40",   "name": "JSE Top 40",       "region": "Africa",  "country": "ZA", "value": 73500.0, "ch_pct":  0.10, "ytd_pct":  11.5},
]


# ═══════════════ FEAR & GREED INDEX ═══════════════
FEAR_GREED_INDEX = {
    "value": 64,
    "level": "Greed",
    "previous_close": 62,
    "one_week_ago": 58,
    "one_month_ago": 51,
    "one_year_ago": 45,
    "components": {
        "market_momentum":      {"value": 72, "level": "Greed"},
        "stock_price_strength": {"value": 68, "level": "Greed"},
        "stock_price_breadth":  {"value": 60, "level": "Greed"},
        "put_call_ratio":       {"value": 55, "level": "Neutral"},
        "junk_bond_demand":     {"value": 70, "level": "Greed"},
        "market_volatility":    {"value": 76, "level": "Extreme Greed"},
        "safe_haven_demand":    {"value": 47, "level": "Neutral"},
    },
}


# ═══════════════ US TREASURY YIELD CURVE ═══════════════
YIELD_CURVE_US = [
    {"tenor": "1M",  "yield": 4.78, "ch_bp": -2},
    {"tenor": "3M",  "yield": 4.65, "ch_bp": -1},
    {"tenor": "6M",  "yield": 4.45, "ch_bp": -1},
    {"tenor": "1Y",  "yield": 4.10, "ch_bp": -2},
    {"tenor": "2Y",  "yield": 3.62, "ch_bp": -3},
    {"tenor": "3Y",  "yield": 3.51, "ch_bp": -3},
    {"tenor": "5Y",  "yield": 3.55, "ch_bp": -2},
    {"tenor": "7Y",  "yield": 3.68, "ch_bp": -2},
    {"tenor": "10Y", "yield": 3.78, "ch_bp": -1},
    {"tenor": "20Y", "yield": 4.12, "ch_bp":  0},
    {"tenor": "30Y", "yield": 4.10, "ch_bp":  1},
]


# ═══════════════ GLOBAL 10Y BOND YIELDS ═══════════════
GLOBAL_BOND_YIELDS = [
    {"country": "US", "tenor": "10Y", "yield": 3.78, "ch_bp": -1},
    {"country": "DE", "tenor": "10Y", "yield": 2.18, "ch_bp": -1},
    {"country": "GB", "tenor": "10Y", "yield": 4.02, "ch_bp": -2},
    {"country": "FR", "tenor": "10Y", "yield": 2.95, "ch_bp": -1},
    {"country": "IT", "tenor": "10Y", "yield": 3.55, "ch_bp": -2},
    {"country": "ES", "tenor": "10Y", "yield": 2.95, "ch_bp": -1},
    {"country": "JP", "tenor": "10Y", "yield": 0.85, "ch_bp":  1},
    {"country": "CN", "tenor": "10Y", "yield": 2.15, "ch_bp": -1},
    {"country": "IN", "tenor": "10Y", "yield": 6.78, "ch_bp": -1},
    {"country": "BR", "tenor": "10Y", "yield": 12.45,"ch_bp":  3},
    {"country": "MX", "tenor": "10Y", "yield": 9.65, "ch_bp":  2},
    {"country": "TR", "tenor": "10Y", "yield": 28.90,"ch_bp": 10},
    {"country": "RU", "tenor": "10Y", "yield": 16.20,"ch_bp":  5},
    {"country": "CA", "tenor": "10Y", "yield": 2.95, "ch_bp": -1},
    {"country": "AU", "tenor": "10Y", "yield": 3.90, "ch_bp":  0},
    {"country": "ZA", "tenor": "10Y", "yield": 9.55, "ch_bp":  2},
]


# ═══════════════ COMMODITY PRICES ═══════════════
COMMODITY_PRICES = [
    {"symbol": "CL",  "name": "WTI Crude Oil",   "category": "energy",     "value":  72.50, "unit": "$/bbl",   "ch_pct":  0.85, "ytd_pct":  1.4},
    {"symbol": "BZ",  "name": "Brent Crude",     "category": "energy",     "value":  76.30, "unit": "$/bbl",   "ch_pct":  0.75, "ytd_pct":  1.2},
    {"symbol": "NG",  "name": "Natural Gas",     "category": "energy",     "value":   2.85, "unit": "$/MMBtu", "ch_pct": -1.10, "ytd_pct": 13.5},
    {"symbol": "HO",  "name": "Heating Oil",     "category": "energy",     "value":   2.21, "unit": "$/gal",   "ch_pct":  0.40, "ytd_pct": -8.5},
    {"symbol": "RB",  "name": "RBOB Gasoline",   "category": "energy",     "value":   2.05, "unit": "$/gal",   "ch_pct":  0.55, "ytd_pct": -2.5},
    {"symbol": "GC",  "name": "Gold",            "category": "metals",     "value": 2645.0, "unit": "$/oz",    "ch_pct":  0.30, "ytd_pct": 28.2},
    {"symbol": "SI",  "name": "Silver",          "category": "metals",     "value":  31.20, "unit": "$/oz",    "ch_pct":  0.60, "ytd_pct": 31.5},
    {"symbol": "PL",  "name": "Platinum",        "category": "metals",     "value":  990.0, "unit": "$/oz",    "ch_pct":  0.20, "ytd_pct":  0.1},
    {"symbol": "PA",  "name": "Palladium",       "category": "metals",     "value": 1020.0, "unit": "$/oz",    "ch_pct": -0.30, "ytd_pct":  0.5},
    {"symbol": "HG",  "name": "Copper",          "category": "metals",     "value":   4.45, "unit": "$/lb",    "ch_pct":  0.85, "ytd_pct": 14.0},
    {"symbol": "ALI", "name": "Aluminum",        "category": "metals",     "value": 2620.0, "unit": "$/t",     "ch_pct":  0.40, "ytd_pct": 12.0},
    {"symbol": "ZN",  "name": "Zinc",            "category": "metals",     "value": 3060.0, "unit": "$/t",     "ch_pct":  0.50, "ytd_pct": 14.5},
    {"symbol": "NI",  "name": "Nickel",          "category": "metals",     "value":17200.0, "unit": "$/t",     "ch_pct": -0.30, "ytd_pct":  4.5},
    {"symbol": "URA", "name": "Uranium U3O8",    "category": "metals",     "value":  82.50, "unit": "$/lb",    "ch_pct":  0.20, "ytd_pct": -8.0},
    {"symbol": "ZW",  "name": "Wheat",           "category": "agriculture","value": 580.0,  "unit": "¢/bu",    "ch_pct": -0.40, "ytd_pct": -7.5},
    {"symbol": "ZC",  "name": "Corn",            "category": "agriculture","value": 412.0,  "unit": "¢/bu",    "ch_pct": -0.20, "ytd_pct":-13.0},
    {"symbol": "ZS",  "name": "Soybeans",        "category": "agriculture","value":1015.0,  "unit": "¢/bu",    "ch_pct": -0.10, "ytd_pct":-21.5},
    {"symbol": "KC",  "name": "Coffee",          "category": "agriculture","value": 268.0,  "unit": "¢/lb",    "ch_pct":  1.20, "ytd_pct": 41.5},
    {"symbol": "CC",  "name": "Cocoa",           "category": "agriculture","value":7850.0,  "unit": "$/t",     "ch_pct":  0.50, "ytd_pct": 84.5},
    {"symbol": "SB",  "name": "Sugar",           "category": "agriculture","value":  22.40, "unit": "¢/lb",    "ch_pct":  0.30, "ytd_pct":  9.0},
    {"symbol": "CT",  "name": "Cotton",          "category": "agriculture","value":  72.50, "unit": "¢/lb",    "ch_pct": -0.20, "ytd_pct": -8.5},
    {"symbol": "LE",  "name": "Live Cattle",    "category": "agriculture","value": 184.5,  "unit": "¢/lb",    "ch_pct":  0.10, "ytd_pct":  9.5},
    {"symbol": "LH",  "name": "Lean Hogs",       "category": "agriculture","value":  84.0,  "unit": "¢/lb",    "ch_pct": -0.30, "ytd_pct":  6.5},
    {"symbol": "LBR", "name": "Lumber",          "category": "agriculture","value": 510.0,  "unit": "$/kbf",   "ch_pct":  1.40, "ytd_pct":-11.5},
]


# ═══════════════ ETF FLOWS (broad) ═══════════════
ETF_FLOWS = [
    {"symbol": "SPY",  "name": "SPDR S&P 500",                "category": "us_equity", "aum_b": 580.0, "flow_5d_b":  1.20, "flow_ytd_b":  8.5},
    {"symbol": "VOO",  "name": "Vanguard S&P 500",            "category": "us_equity", "aum_b": 510.0, "flow_5d_b":  2.10, "flow_ytd_b": 72.5},
    {"symbol": "IVV",  "name": "iShares Core S&P 500",        "category": "us_equity", "aum_b": 480.0, "flow_5d_b":  1.45, "flow_ytd_b": 41.0},
    {"symbol": "QQQ",  "name": "Invesco QQQ (Nasdaq 100)",    "category": "us_equity", "aum_b": 295.0, "flow_5d_b":  0.85, "flow_ytd_b": 19.0},
    {"symbol": "VTI",  "name": "Vanguard Total Stock Market", "category": "us_equity", "aum_b": 410.0, "flow_5d_b":  1.10, "flow_ytd_b": 20.5},
    {"symbol": "IWM",  "name": "iShares Russell 2000",        "category": "us_equity", "aum_b":  60.0, "flow_5d_b": -0.30, "flow_ytd_b": -3.5},
    {"symbol": "VEA",  "name": "Vanguard FTSE Developed",     "category": "intl_eq",   "aum_b": 130.0, "flow_5d_b":  0.45, "flow_ytd_b":  8.0},
    {"symbol": "VWO",  "name": "Vanguard FTSE Emerging Mkts", "category": "intl_eq",   "aum_b":  85.0, "flow_5d_b":  0.40, "flow_ytd_b":  5.5},
    {"symbol": "EEM",  "name": "iShares MSCI Emerging Mkts",  "category": "intl_eq",   "aum_b":  17.5, "flow_5d_b":  0.30, "flow_ytd_b":  1.2},
    {"symbol": "FXI",  "name": "iShares China Large-Cap",     "category": "intl_eq",   "aum_b":   6.5, "flow_5d_b":  0.35, "flow_ytd_b":  0.8},
    {"symbol": "AGG",  "name": "iShares US Aggregate Bond",   "category": "fixed_inc", "aum_b": 115.0, "flow_5d_b":  0.50, "flow_ytd_b":  4.5},
    {"symbol": "BND",  "name": "Vanguard Total Bond Market",  "category": "fixed_inc", "aum_b": 120.0, "flow_5d_b":  0.55, "flow_ytd_b": 16.5},
    {"symbol": "TLT",  "name": "iShares 20+ Year Treasury",   "category": "fixed_inc", "aum_b":  60.0, "flow_5d_b":  0.45, "flow_ytd_b":  6.0},
    {"symbol": "HYG",  "name": "iShares iBoxx HY Corp",       "category": "fixed_inc", "aum_b":  17.5, "flow_5d_b":  0.05, "flow_ytd_b": -1.5},
    {"symbol": "GLD",  "name": "SPDR Gold Shares",            "category": "commodity", "aum_b":  78.0, "flow_5d_b":  0.65, "flow_ytd_b":  2.5},
    {"symbol": "IAU",  "name": "iShares Gold Trust",          "category": "commodity", "aum_b":  32.0, "flow_5d_b":  0.30, "flow_ytd_b":  1.5},
    {"symbol": "SLV",  "name": "iShares Silver Trust",        "category": "commodity", "aum_b":  13.0, "flow_5d_b":  0.10, "flow_ytd_b":  0.4},
    {"symbol": "USO",  "name": "US Oil Fund",                 "category": "commodity", "aum_b":   1.4, "flow_5d_b": -0.05, "flow_ytd_b": -0.5},
]


# ═══════════════ EARNINGS CALENDAR ═══════════════
EARNINGS_CALENDAR = [
    {"ticker": "AAPL",  "name": "Apple",          "date": "2026-04-30", "session": "AMC", "eps_est":  1.55, "rev_est_b":  98.5,  "importance": "high"},
    {"ticker": "MSFT",  "name": "Microsoft",      "date": "2026-04-23", "session": "AMC", "eps_est":  3.10, "rev_est_b":  68.5,  "importance": "high"},
    {"ticker": "GOOGL", "name": "Alphabet",       "date": "2026-04-24", "session": "AMC", "eps_est":  2.05, "rev_est_b":  88.5,  "importance": "high"},
    {"ticker": "AMZN",  "name": "Amazon",         "date": "2026-05-01", "session": "AMC", "eps_est":  1.40, "rev_est_b": 156.0,  "importance": "high"},
    {"ticker": "META",  "name": "Meta Platforms", "date": "2026-04-30", "session": "AMC", "eps_est":  5.40, "rev_est_b":  41.5,  "importance": "high"},
    {"ticker": "NVDA",  "name": "NVIDIA",         "date": "2026-05-21", "session": "AMC", "eps_est":  0.85, "rev_est_b":  46.5,  "importance": "high"},
    {"ticker": "TSLA",  "name": "Tesla",          "date": "2026-04-22", "session": "AMC", "eps_est":  0.55, "rev_est_b":  24.5,  "importance": "high"},
    {"ticker": "AVGO",  "name": "Broadcom",       "date": "2026-06-05", "session": "AMC", "eps_est":  1.65, "rev_est_b":  14.0,  "importance": "high"},
    {"ticker": "TSM",   "name": "TSMC",           "date": "2026-04-17", "session": "BMO", "eps_est":  2.05, "rev_est_b":  25.5,  "importance": "high"},
    {"ticker": "JPM",   "name": "JPMorgan",       "date": "2026-04-12", "session": "BMO", "eps_est":  4.25, "rev_est_b":  41.5,  "importance": "high"},
    {"ticker": "BAC",   "name": "Bank of America","date": "2026-04-15", "session": "BMO", "eps_est":  0.81, "rev_est_b":  25.5,  "importance": "high"},
    {"ticker": "WFC",   "name": "Wells Fargo",    "date": "2026-04-12", "session": "BMO", "eps_est":  1.20, "rev_est_b":  20.0,  "importance": "med"},
    {"ticker": "C",     "name": "Citigroup",      "date": "2026-04-12", "session": "BMO", "eps_est":  1.30, "rev_est_b":  20.5,  "importance": "med"},
    {"ticker": "GS",    "name": "Goldman Sachs",  "date": "2026-04-15", "session": "BMO", "eps_est":  8.55, "rev_est_b":  12.5,  "importance": "high"},
    {"ticker": "MS",    "name": "Morgan Stanley", "date": "2026-04-16", "session": "BMO", "eps_est":  1.65, "rev_est_b":  14.5,  "importance": "med"},
    {"ticker": "BLK",   "name": "BlackRock",      "date": "2026-04-12", "session": "BMO", "eps_est": 10.05, "rev_est_b":   4.7,  "importance": "med"},
    {"ticker": "JNJ",   "name": "Johnson&Johnson","date": "2026-04-16", "session": "BMO", "eps_est":  2.50, "rev_est_b":  21.5,  "importance": "high"},
    {"ticker": "PG",    "name": "Procter&Gamble", "date": "2026-04-19", "session": "BMO", "eps_est":  1.40, "rev_est_b":  20.0,  "importance": "med"},
    {"ticker": "KO",    "name": "Coca-Cola",      "date": "2026-04-30", "session": "BMO", "eps_est":  0.69, "rev_est_b":  11.0,  "importance": "med"},
    {"ticker": "PEP",   "name": "PepsiCo",        "date": "2026-04-23", "session": "BMO", "eps_est":  1.55, "rev_est_b":  18.0,  "importance": "med"},
    {"ticker": "XOM",   "name": "Exxon Mobil",    "date": "2026-05-03", "session": "BMO", "eps_est":  2.05, "rev_est_b":  85.5,  "importance": "high"},
    {"ticker": "CVX",   "name": "Chevron",        "date": "2026-05-03", "session": "BMO", "eps_est":  2.85, "rev_est_b":  48.5,  "importance": "high"},
    {"ticker": "LMT",   "name": "Lockheed Martin","date": "2026-04-23", "session": "BMO", "eps_est":  6.45, "rev_est_b":  17.0,  "importance": "med"},
    {"ticker": "RTX",   "name": "RTX Corp",       "date": "2026-04-23", "session": "BMO", "eps_est":  1.35, "rev_est_b":  20.0,  "importance": "med"},
    {"ticker": "BA",    "name": "Boeing",         "date": "2026-04-24", "session": "BMO", "eps_est": -1.85, "rev_est_b":  17.0,  "importance": "high"},
]


# ═══════════════ COT REPORT (Commitments of Traders) ═══════════════
COT_REPORT = [
    {"contract": "S&P 500 E-mini",   "category": "equity_index","large_spec_long": 920000, "large_spec_short": 1010000, "net_position":  -90000, "ch_week":  -8500},
    {"contract": "Nasdaq 100 E-mini","category": "equity_index","large_spec_long": 250000, "large_spec_short":  235000, "net_position":   15000, "ch_week":   2300},
    {"contract": "Russell 2000",     "category": "equity_index","large_spec_long":  64000, "large_spec_short":   78000, "net_position":  -14000, "ch_week":  -2100},
    {"contract": "10Y Treasury Note","category": "rates",       "large_spec_long": 605000, "large_spec_short":  720000, "net_position": -115000, "ch_week":  12500},
    {"contract": "30Y Treasury Bond","category": "rates",       "large_spec_long": 220000, "large_spec_short":  280000, "net_position":  -60000, "ch_week":   3500},
    {"contract": "Eurodollar/SOFR",  "category": "rates",       "large_spec_long": 905000, "large_spec_short":  720000, "net_position":  185000, "ch_week":  18000},
    {"contract": "Gold",             "category": "metals",      "large_spec_long": 245000, "large_spec_short":   45000, "net_position":  200000, "ch_week":   3500},
    {"contract": "Silver",           "category": "metals",      "large_spec_long":  75000, "large_spec_short":   25000, "net_position":   50000, "ch_week":   1200},
    {"contract": "Copper",           "category": "metals",      "large_spec_long":  82000, "large_spec_short":   45000, "net_position":   37000, "ch_week":   2100},
    {"contract": "WTI Crude Oil",    "category": "energy",      "large_spec_long": 290000, "large_spec_short":   95000, "net_position":  195000, "ch_week":  -4500},
    {"contract": "Natural Gas",      "category": "energy",      "large_spec_long": 215000, "large_spec_short":  385000, "net_position": -170000, "ch_week":  -3500},
    {"contract": "EUR/USD",          "category": "fx",          "large_spec_long": 198000, "large_spec_short":  130000, "net_position":   68000, "ch_week":   1500},
    {"contract": "JPY/USD",          "category": "fx",          "large_spec_long":  85000, "large_spec_short":  155000, "net_position":  -70000, "ch_week":   8500},
    {"contract": "GBP/USD",          "category": "fx",          "large_spec_long":  90000, "large_spec_short":   55000, "net_position":   35000, "ch_week":    900},
    {"contract": "Wheat",            "category": "agriculture", "large_spec_long":  85000, "large_spec_short":  130000, "net_position":  -45000, "ch_week":  -1500},
    {"contract": "Corn",             "category": "agriculture", "large_spec_long": 320000, "large_spec_short":  385000, "net_position":  -65000, "ch_week":   4500},
    {"contract": "Soybeans",         "category": "agriculture", "large_spec_long": 145000, "large_spec_short":  235000, "net_position":  -90000, "ch_week":  -2500},
]


# ═══════════════ GDELT-STYLE EVENTS ═══════════════
GDELT_EVENTS = [
    {"date": "2026-04-09", "actor1": "USA", "actor2": "CHN", "event_type": "Statement",       "tone": -3.2, "goldstein": -2.0, "summary": "US trade rep criticizes Beijing semiconductor controls"},
    {"date": "2026-04-09", "actor1": "RUS", "actor2": "UKR", "event_type": "Military Action", "tone": -7.5, "goldstein": -8.5, "summary": "Drone strikes reported on Kharkiv power infrastructure"},
    {"date": "2026-04-09", "actor1": "ISR", "actor2": "LBN", "event_type": "Diplomatic",      "tone": -1.2, "goldstein":  1.0, "summary": "Cross-border talks announced via French mediation"},
    {"date": "2026-04-09", "actor1": "IRN", "actor2": "USA", "event_type": "Statement",       "tone": -4.5, "goldstein": -1.0, "summary": "Tehran rejects new IAEA inspection terms"},
    {"date": "2026-04-09", "actor1": "PRK", "actor2": "KOR", "event_type": "Military Posture","tone": -5.5, "goldstein": -3.0, "summary": "Pyongyang launches short-range ballistic test"},
    {"date": "2026-04-09", "actor1": "CHN", "actor2": "TWN", "event_type": "Military Posture","tone": -3.8, "goldstein": -2.0, "summary": "PLAN warships transit Taiwan Strait median"},
    {"date": "2026-04-09", "actor1": "IND", "actor2": "PAK", "event_type": "Statement",       "tone": -2.1, "goldstein": -1.0, "summary": "Border tensions escalate after LOC firing exchange"},
    {"date": "2026-04-09", "actor1": "TUR", "actor2": "GRC", "event_type": "Diplomatic",      "tone":  0.5, "goldstein":  2.0, "summary": "Athens-Ankara energy dispute talks resume"},
    {"date": "2026-04-09", "actor1": "VEN", "actor2": "GUY", "event_type": "Statement",       "tone": -3.5, "goldstein": -2.0, "summary": "Caracas reasserts Essequibo claim ahead of vote"},
    {"date": "2026-04-09", "actor1": "ETH", "actor2": "ERI", "event_type": "Diplomatic",      "tone": -1.5, "goldstein": -1.0, "summary": "Tigray border tensions return after peace deal lapses"},
    {"date": "2026-04-09", "actor1": "FRA", "actor2": "MLI", "event_type": "Diplomatic",      "tone": -2.5, "goldstein": -1.0, "summary": "Paris withdraws remaining advisors after junta ultimatum"},
    {"date": "2026-04-09", "actor1": "DEU", "actor2": "RUS", "event_type": "Statement",       "tone": -3.0, "goldstein": -1.0, "summary": "Berlin condemns continued targeting of civilians"},
    {"date": "2026-04-09", "actor1": "JPN", "actor2": "CHN", "event_type": "Statement",       "tone": -1.8, "goldstein": -1.0, "summary": "Tokyo files protest over Senkaku coast guard incursion"},
    {"date": "2026-04-09", "actor1": "AUS", "actor2": "USA", "event_type": "Cooperation",     "tone":  4.5, "goldstein":  6.0, "summary": "AUKUS sub deal accelerates production timeline"},
    {"date": "2026-04-09", "actor1": "SAU", "actor2": "IRN", "event_type": "Diplomatic",      "tone":  2.0, "goldstein":  3.0, "summary": "Riyadh-Tehran rapprochement extended to security matters"},
    {"date": "2026-04-09", "actor1": "EGY", "actor2": "ETH", "event_type": "Statement",       "tone": -2.5, "goldstein": -1.0, "summary": "Cairo warns of GERD fourth filling consequences"},
    {"date": "2026-04-09", "actor1": "POL", "actor2": "BLR", "event_type": "Border Incident", "tone": -3.0, "goldstein": -2.0, "summary": "Polish border guard reports migrant push attempt"},
    {"date": "2026-04-09", "actor1": "MDA", "actor2": "RUS", "event_type": "Statement",       "tone": -2.8, "goldstein": -1.0, "summary": "Chisinau warns of hybrid attacks ahead of EU vote"},
    {"date": "2026-04-09", "actor1": "GBR", "actor2": "EU",  "event_type": "Cooperation",     "tone":  3.5, "goldstein":  4.0, "summary": "London-Brussels defence pact talks advance"},
    {"date": "2026-04-09", "actor1": "MEX", "actor2": "USA", "event_type": "Diplomatic",      "tone":  1.5, "goldstein":  2.0, "summary": "Cross-border fentanyl task force expanded"},
    {"date": "2026-04-09", "actor1": "BRA", "actor2": "ARG", "event_type": "Cooperation",     "tone":  2.5, "goldstein":  3.0, "summary": "Mercosur trade bloc reform talks begin"},
    {"date": "2026-04-09", "actor1": "ZAF", "actor2": "USA", "event_type": "Statement",       "tone": -2.0, "goldstein": -1.0, "summary": "Pretoria responds to AGOA review concerns"},
    {"date": "2026-04-09", "actor1": "VNM", "actor2": "CHN", "event_type": "Statement",       "tone": -2.5, "goldstein": -1.0, "summary": "Hanoi protests new South China Sea baseline"},
    {"date": "2026-04-09", "actor1": "PHL", "actor2": "CHN", "event_type": "Naval Incident",  "tone": -4.0, "goldstein": -3.0, "summary": "Coast guard collision near Scarborough Shoal"},
    {"date": "2026-04-09", "actor1": "HUN", "actor2": "EU",  "event_type": "Statement",       "tone": -2.0, "goldstein": -1.0, "summary": "Budapest blocks new aid tranche for Kyiv"},
    {"date": "2026-04-09", "actor1": "SDN", "actor2": "TCD", "event_type": "Refugee Flow",    "tone": -5.0, "goldstein": -3.0, "summary": "200K cross border into Chad amid Darfur fighting"},
    {"date": "2026-04-09", "actor1": "YEM", "actor2": "ISR", "event_type": "Military Action", "tone": -6.0, "goldstein": -7.0, "summary": "Houthi missile intercepted over Eilat"},
    {"date": "2026-04-09", "actor1": "SYR", "actor2": "TUR", "event_type": "Diplomatic",      "tone": -1.5, "goldstein": -1.0, "summary": "Ankara-Damascus normalization talks stall"},
    {"date": "2026-04-09", "actor1": "GEO", "actor2": "RUS", "event_type": "Statement",       "tone": -2.5, "goldstein": -1.0, "summary": "Tbilisi warns of foreign agent law backlash"},
    {"date": "2026-04-09", "actor1": "NIC", "actor2": "CRI", "event_type": "Border Incident", "tone": -2.0, "goldstein": -1.0, "summary": "Cross-border incursion reported in Río San Juan"},
]


# ═══════════════ GLOBAL CONFLICT INDEX (per country) ═══════════════
GLOBAL_CONFLICT_INDEX = [
    {"country": "Ukraine",        "iso": "UA", "score": 98, "trend": "high", "events_30d": 1840, "fatalities_30d": 4250},
    {"country": "Russia",         "iso": "RU", "score": 96, "trend": "high", "events_30d": 1620, "fatalities_30d": 3850},
    {"country": "Sudan",          "iso": "SD", "score": 95, "trend": "high", "events_30d": 1240, "fatalities_30d": 2150},
    {"country": "Gaza/Palestine", "iso": "PS", "score": 94, "trend": "high", "events_30d":  980, "fatalities_30d": 1850},
    {"country": "Israel",         "iso": "IL", "score": 88, "trend": "high", "events_30d":  720, "fatalities_30d":  220},
    {"country": "Syria",          "iso": "SY", "score": 86, "trend": "med",  "events_30d":  640, "fatalities_30d":  580},
    {"country": "Yemen",          "iso": "YE", "score": 85, "trend": "high", "events_30d":  550, "fatalities_30d":  410},
    {"country": "Myanmar",        "iso": "MM", "score": 84, "trend": "high", "events_30d":  720, "fatalities_30d":  680},
    {"country": "DR Congo",       "iso": "CD", "score": 83, "trend": "high", "events_30d":  610, "fatalities_30d":  720},
    {"country": "Somalia",        "iso": "SO", "score": 82, "trend": "med",  "events_30d":  490, "fatalities_30d":  340},
    {"country": "Mali",           "iso": "ML", "score": 80, "trend": "med",  "events_30d":  410, "fatalities_30d":  280},
    {"country": "Burkina Faso",   "iso": "BF", "score": 79, "trend": "high", "events_30d":  390, "fatalities_30d":  310},
    {"country": "Nigeria",        "iso": "NG", "score": 76, "trend": "med",  "events_30d":  520, "fatalities_30d":  390},
    {"country": "Lebanon",        "iso": "LB", "score": 75, "trend": "high", "events_30d":  280, "fatalities_30d":  150},
    {"country": "Iran",           "iso": "IR", "score": 73, "trend": "med",  "events_30d":  220, "fatalities_30d":   45},
    {"country": "Afghanistan",    "iso": "AF", "score": 72, "trend": "med",  "events_30d":  340, "fatalities_30d":  185},
    {"country": "Iraq",           "iso": "IQ", "score": 70, "trend": "med",  "events_30d":  260, "fatalities_30d":   95},
    {"country": "Mozambique",     "iso": "MZ", "score": 68, "trend": "med",  "events_30d":  220, "fatalities_30d":  140},
    {"country": "Ethiopia",       "iso": "ET", "score": 67, "trend": "med",  "events_30d":  280, "fatalities_30d":  175},
    {"country": "Cameroon",       "iso": "CM", "score": 65, "trend": "low",  "events_30d":  180, "fatalities_30d":   85},
    {"country": "Pakistan",       "iso": "PK", "score": 64, "trend": "med",  "events_30d":  240, "fatalities_30d":  120},
    {"country": "Venezuela",      "iso": "VE", "score": 62, "trend": "low",  "events_30d":  150, "fatalities_30d":   25},
    {"country": "Colombia",       "iso": "CO", "score": 60, "trend": "low",  "events_30d":  210, "fatalities_30d":   95},
    {"country": "Mexico",         "iso": "MX", "score": 60, "trend": "med",  "events_30d":  340, "fatalities_30d":  280},
    {"country": "Haiti",          "iso": "HT", "score": 58, "trend": "high", "events_30d":  170, "fatalities_30d":  140},
]


# ═══════════════ HUMANITARIAN CRISES ═══════════════
HUMANITARIAN_CRISES = [
    {"country": "Sudan",          "iso": "SD", "people_in_need_m": 24.8, "displaced_m": 10.7, "food_insecure_m": 17.7, "funding_required_b": 4.1, "funding_pct": 28},
    {"country": "Yemen",          "iso": "YE", "people_in_need_m": 18.2, "displaced_m":  4.5, "food_insecure_m": 17.0, "funding_required_b": 2.7, "funding_pct": 35},
    {"country": "Afghanistan",    "iso": "AF", "people_in_need_m": 23.7, "displaced_m":  3.2, "food_insecure_m": 15.8, "funding_required_b": 3.0, "funding_pct": 32},
    {"country": "Syria",          "iso": "SY", "people_in_need_m": 16.7, "displaced_m":  7.2, "food_insecure_m": 12.9, "funding_required_b": 4.1, "funding_pct": 30},
    {"country": "Ethiopia",       "iso": "ET", "people_in_need_m": 21.4, "displaced_m":  4.4, "food_insecure_m": 15.8, "funding_required_b": 3.2, "funding_pct": 38},
    {"country": "Ukraine",        "iso": "UA", "people_in_need_m": 14.6, "displaced_m":  5.9, "food_insecure_m":  7.8, "funding_required_b": 3.1, "funding_pct": 42},
    {"country": "Gaza/Palestine", "iso": "PS", "people_in_need_m":  3.0, "displaced_m":  1.9, "food_insecure_m":  2.2, "funding_required_b": 2.8, "funding_pct": 41},
    {"country": "DR Congo",       "iso": "CD", "people_in_need_m": 25.4, "displaced_m":  6.3, "food_insecure_m": 25.8, "funding_required_b": 2.6, "funding_pct": 33},
    {"country": "Somalia",        "iso": "SO", "people_in_need_m":  6.9, "displaced_m":  3.9, "food_insecure_m":  4.4, "funding_required_b": 1.6, "funding_pct": 36},
    {"country": "Myanmar",        "iso": "MM", "people_in_need_m": 18.6, "displaced_m":  3.4, "food_insecure_m": 12.9, "funding_required_b": 1.0, "funding_pct": 22},
    {"country": "Nigeria",        "iso": "NG", "people_in_need_m":  8.3, "displaced_m":  3.4, "food_insecure_m": 26.5, "funding_required_b": 0.9, "funding_pct": 41},
    {"country": "Burkina Faso",   "iso": "BF", "people_in_need_m":  6.3, "displaced_m":  2.1, "food_insecure_m":  3.4, "funding_required_b": 0.8, "funding_pct": 28},
    {"country": "Mali",           "iso": "ML", "people_in_need_m":  7.1, "displaced_m":  0.4, "food_insecure_m":  1.4, "funding_required_b": 0.7, "funding_pct": 32},
    {"country": "Haiti",          "iso": "HT", "people_in_need_m":  5.5, "displaced_m":  0.6, "food_insecure_m":  4.4, "funding_required_b": 0.7, "funding_pct": 25},
    {"country": "Venezuela",      "iso": "VE", "people_in_need_m":  7.7, "displaced_m":  0.4, "food_insecure_m":  6.5, "funding_required_b": 0.6, "funding_pct": 30},
]


# ═══════════════ WORLD CLOCK ZONES ═══════════════
WORLD_CLOCK_ZONES = [
    {"city": "New York",     "country": "US", "tz": "America/New_York",     "utc_offset": -5, "lat":  40.71, "lng":  -74.01, "is_dst": True},
    {"city": "Chicago",      "country": "US", "tz": "America/Chicago",      "utc_offset": -6, "lat":  41.88, "lng":  -87.63, "is_dst": True},
    {"city": "Los Angeles",  "country": "US", "tz": "America/Los_Angeles",  "utc_offset": -8, "lat":  34.05, "lng": -118.24, "is_dst": True},
    {"city": "Anchorage",    "country": "US", "tz": "America/Anchorage",    "utc_offset": -9, "lat":  61.22, "lng": -149.90, "is_dst": True},
    {"city": "Honolulu",     "country": "US", "tz": "Pacific/Honolulu",     "utc_offset": -10,"lat":  21.31, "lng": -157.86, "is_dst": False},
    {"city": "Toronto",      "country": "CA", "tz": "America/Toronto",      "utc_offset": -5, "lat":  43.65, "lng":  -79.38, "is_dst": True},
    {"city": "Mexico City",  "country": "MX", "tz": "America/Mexico_City",  "utc_offset": -6, "lat":  19.43, "lng":  -99.13, "is_dst": False},
    {"city": "São Paulo",    "country": "BR", "tz": "America/Sao_Paulo",    "utc_offset": -3, "lat": -23.55, "lng":  -46.63, "is_dst": False},
    {"city": "Buenos Aires", "country": "AR", "tz": "America/Buenos_Aires", "utc_offset": -3, "lat": -34.60, "lng":  -58.38, "is_dst": False},
    {"city": "London",       "country": "GB", "tz": "Europe/London",        "utc_offset":  1, "lat":  51.51, "lng":   -0.13, "is_dst": True},
    {"city": "Dublin",       "country": "IE", "tz": "Europe/Dublin",        "utc_offset":  1, "lat":  53.35, "lng":   -6.26, "is_dst": True},
    {"city": "Paris",        "country": "FR", "tz": "Europe/Paris",         "utc_offset":  2, "lat":  48.86, "lng":    2.35, "is_dst": True},
    {"city": "Berlin",       "country": "DE", "tz": "Europe/Berlin",        "utc_offset":  2, "lat":  52.52, "lng":   13.41, "is_dst": True},
    {"city": "Rome",         "country": "IT", "tz": "Europe/Rome",          "utc_offset":  2, "lat":  41.90, "lng":   12.50, "is_dst": True},
    {"city": "Madrid",       "country": "ES", "tz": "Europe/Madrid",        "utc_offset":  2, "lat":  40.42, "lng":   -3.70, "is_dst": True},
    {"city": "Moscow",       "country": "RU", "tz": "Europe/Moscow",        "utc_offset":  3, "lat":  55.76, "lng":   37.62, "is_dst": False},
    {"city": "Istanbul",     "country": "TR", "tz": "Europe/Istanbul",      "utc_offset":  3, "lat":  41.01, "lng":   28.98, "is_dst": False},
    {"city": "Dubai",        "country": "AE", "tz": "Asia/Dubai",           "utc_offset":  4, "lat":  25.20, "lng":   55.27, "is_dst": False},
    {"city": "Riyadh",       "country": "SA", "tz": "Asia/Riyadh",          "utc_offset":  3, "lat":  24.69, "lng":   46.72, "is_dst": False},
    {"city": "Tehran",       "country": "IR", "tz": "Asia/Tehran",          "utc_offset":  3, "lat":  35.69, "lng":   51.39, "is_dst": False},
    {"city": "Mumbai",       "country": "IN", "tz": "Asia/Kolkata",         "utc_offset":  5, "lat":  19.08, "lng":   72.88, "is_dst": False},
    {"city": "Bangkok",      "country": "TH", "tz": "Asia/Bangkok",         "utc_offset":  7, "lat":  13.75, "lng":  100.50, "is_dst": False},
    {"city": "Singapore",    "country": "SG", "tz": "Asia/Singapore",       "utc_offset":  8, "lat":   1.35, "lng":  103.82, "is_dst": False},
    {"city": "Beijing",      "country": "CN", "tz": "Asia/Shanghai",        "utc_offset":  8, "lat":  39.91, "lng":  116.39, "is_dst": False},
    {"city": "Hong Kong",    "country": "HK", "tz": "Asia/Hong_Kong",       "utc_offset":  8, "lat":  22.30, "lng":  114.17, "is_dst": False},
    {"city": "Tokyo",        "country": "JP", "tz": "Asia/Tokyo",           "utc_offset":  9, "lat":  35.68, "lng":  139.69, "is_dst": False},
    {"city": "Seoul",        "country": "KR", "tz": "Asia/Seoul",           "utc_offset":  9, "lat":  37.57, "lng":  126.98, "is_dst": False},
    {"city": "Sydney",       "country": "AU", "tz": "Australia/Sydney",     "utc_offset": 10, "lat": -33.87, "lng":  151.21, "is_dst": False},
    {"city": "Auckland",     "country": "NZ", "tz": "Pacific/Auckland",     "utc_offset": 12, "lat": -36.85, "lng":  174.76, "is_dst": False},
    {"city": "Cape Town",    "country": "ZA", "tz": "Africa/Johannesburg",  "utc_offset":  2, "lat": -33.92, "lng":   18.42, "is_dst": False},
    {"city": "Lagos",        "country": "NG", "tz": "Africa/Lagos",         "utc_offset":  1, "lat":   6.45, "lng":    3.40, "is_dst": False},
    {"city": "Cairo",        "country": "EG", "tz": "Africa/Cairo",         "utc_offset":  3, "lat":  30.04, "lng":   31.24, "is_dst": False},
    {"city": "Reykjavik",    "country": "IS", "tz": "Atlantic/Reykjavik",   "utc_offset":  0, "lat":  64.13, "lng":  -21.94, "is_dst": False},
]


# ═══════════════ NATIONAL DEBT (top 28 economies) ═══════════════
NATIONAL_DEBT = [
    {"country": "United States", "iso": "US", "debt_t": 35.80, "debt_gdp_pct": 122, "debt_pc": 106800, "ch_yoy_pct":  6.5},
    {"country": "China",         "iso": "CN", "debt_t": 14.20, "debt_gdp_pct":  84, "debt_pc":  10100, "ch_yoy_pct":  9.5},
    {"country": "Japan",         "iso": "JP", "debt_t": 10.60, "debt_gdp_pct": 263, "debt_pc":  85100, "ch_yoy_pct":  3.0},
    {"country": "United Kingdom","iso": "GB", "debt_t":  3.45, "debt_gdp_pct": 102, "debt_pc":  50800, "ch_yoy_pct":  4.5},
    {"country": "France",        "iso": "FR", "debt_t":  3.55, "debt_gdp_pct": 112, "debt_pc":  52400, "ch_yoy_pct":  4.0},
    {"country": "Italy",         "iso": "IT", "debt_t":  3.10, "debt_gdp_pct": 137, "debt_pc":  52600, "ch_yoy_pct":  3.5},
    {"country": "Germany",       "iso": "DE", "debt_t":  2.90, "debt_gdp_pct":  64, "debt_pc":  34800, "ch_yoy_pct":  3.0},
    {"country": "India",         "iso": "IN", "debt_t":  3.10, "debt_gdp_pct":  82, "debt_pc":   2200, "ch_yoy_pct":  8.5},
    {"country": "Brazil",        "iso": "BR", "debt_t":  2.05, "debt_gdp_pct":  88, "debt_pc":   9500, "ch_yoy_pct":  9.0},
    {"country": "Canada",        "iso": "CA", "debt_t":  2.20, "debt_gdp_pct": 107, "debt_pc":  56500, "ch_yoy_pct":  3.5},
    {"country": "Spain",         "iso": "ES", "debt_t":  1.65, "debt_gdp_pct": 105, "debt_pc":  34800, "ch_yoy_pct":  3.0},
    {"country": "Mexico",        "iso": "MX", "debt_t":  0.85, "debt_gdp_pct":  53, "debt_pc":   6500, "ch_yoy_pct":  6.5},
    {"country": "Australia",     "iso": "AU", "debt_t":  0.95, "debt_gdp_pct":  56, "debt_pc":  36500, "ch_yoy_pct":  4.5},
    {"country": "South Korea",   "iso": "KR", "debt_t":  0.85, "debt_gdp_pct":  52, "debt_pc":  16500, "ch_yoy_pct":  6.0},
    {"country": "Russia",        "iso": "RU", "debt_t":  0.32, "debt_gdp_pct":  18, "debt_pc":   2200, "ch_yoy_pct":  9.5},
    {"country": "Turkey",        "iso": "TR", "debt_t":  0.42, "debt_gdp_pct":  31, "debt_pc":   4900, "ch_yoy_pct": 14.5},
    {"country": "Indonesia",     "iso": "ID", "debt_t":  0.51, "debt_gdp_pct":  39, "debt_pc":   1850, "ch_yoy_pct":  7.5},
    {"country": "Saudi Arabia",  "iso": "SA", "debt_t":  0.32, "debt_gdp_pct":  29, "debt_pc":   8500, "ch_yoy_pct":  5.5},
    {"country": "Switzerland",   "iso": "CH", "debt_t":  0.18, "debt_gdp_pct":  39, "debt_pc":  20100, "ch_yoy_pct":  1.5},
    {"country": "Netherlands",   "iso": "NL", "debt_t":  0.46, "debt_gdp_pct":  47, "debt_pc":  26000, "ch_yoy_pct":  2.5},
    {"country": "Belgium",       "iso": "BE", "debt_t":  0.62, "debt_gdp_pct": 106, "debt_pc":  53000, "ch_yoy_pct":  3.5},
    {"country": "Greece",        "iso": "GR", "debt_t":  0.41, "debt_gdp_pct": 162, "debt_pc":  39000, "ch_yoy_pct":  2.0},
    {"country": "Argentina",     "iso": "AR", "debt_t":  0.39, "debt_gdp_pct":  87, "debt_pc":   8500, "ch_yoy_pct": 22.5},
    {"country": "Egypt",         "iso": "EG", "debt_t":  0.42, "debt_gdp_pct":  96, "debt_pc":   3800, "ch_yoy_pct": 18.5},
    {"country": "Pakistan",      "iso": "PK", "debt_t":  0.27, "debt_gdp_pct":  77, "debt_pc":   1100, "ch_yoy_pct": 16.5},
    {"country": "Nigeria",       "iso": "NG", "debt_t":  0.16, "debt_gdp_pct":  46, "debt_pc":    750, "ch_yoy_pct": 12.5},
    {"country": "South Africa",  "iso": "ZA", "debt_t":  0.27, "debt_gdp_pct":  74, "debt_pc":   4500, "ch_yoy_pct":  9.5},
    {"country": "Singapore",     "iso": "SG", "debt_t":  0.51, "debt_gdp_pct": 158, "debt_pc":  86500, "ch_yoy_pct":  3.0},
]


# ═══════════════ CRITICAL INFRASTRUCTURE ═══════════════
# Major data centers, undersea cable landing points, oil pipelines, nuclear reactors, capital cities

CAPITAL_CITIES = [
    {"name": "Washington D.C.", "country": "US", "lat": 38.90, "lng": -77.04, "pop": "5.4M metro"},
    {"name": "Beijing", "country": "CN", "lat": 39.91, "lng": 116.39, "pop": "21.5M"},
    {"name": "Moscow", "country": "RU", "lat": 55.76, "lng": 37.62, "pop": "12.6M"},
    {"name": "London", "country": "GB", "lat": 51.51, "lng": -0.13, "pop": "9.5M"},
    {"name": "Paris", "country": "FR", "lat": 48.86, "lng": 2.35, "pop": "11M metro"},
    {"name": "Berlin", "country": "DE", "lat": 52.52, "lng": 13.41, "pop": "3.7M"},
    {"name": "Tokyo", "country": "JP", "lat": 35.68, "lng": 139.69, "pop": "37.4M metro"},
    {"name": "New Delhi", "country": "IN", "lat": 28.61, "lng": 77.21, "pop": "32M metro"},
    {"name": "Brasília", "country": "BR", "lat": -15.78, "lng": -47.93, "pop": "4.8M metro"},
    {"name": "Canberra", "country": "AU", "lat": -35.28, "lng": 149.13, "pop": "470K"},
    {"name": "Ottawa", "country": "CA", "lat": 45.42, "lng": -75.70, "pop": "1.4M metro"},
    {"name": "Seoul", "country": "KR", "lat": 37.57, "lng": 126.98, "pop": "9.7M"},
    {"name": "Pyongyang", "country": "KP", "lat": 39.02, "lng": 125.75, "pop": "3.2M"},
    {"name": "Taipei", "country": "TW", "lat": 25.03, "lng": 121.57, "pop": "2.6M"},
    {"name": "Ankara", "country": "TR", "lat": 39.93, "lng": 32.85, "pop": "5.7M"},
    {"name": "Riyadh", "country": "SA", "lat": 24.69, "lng": 46.72, "pop": "7.7M"},
    {"name": "Tehran", "country": "IR", "lat": 35.69, "lng": 51.39, "pop": "9.4M"},
    {"name": "Jerusalem", "country": "IL", "lat": 31.77, "lng": 35.23, "pop": "970K"},
    {"name": "Cairo", "country": "EG", "lat": 30.04, "lng": 31.24, "pop": "21M metro"},
    {"name": "Kyiv", "country": "UA", "lat": 50.45, "lng": 30.52, "pop": "3M"},
    {"name": "Rome", "country": "IT", "lat": 41.90, "lng": 12.50, "pop": "4.3M metro"},
    {"name": "Madrid", "country": "ES", "lat": 40.42, "lng": -3.70, "pop": "6.7M metro"},
    {"name": "Warsaw", "country": "PL", "lat": 52.23, "lng": 21.01, "pop": "1.8M"},
    {"name": "Nairobi", "country": "KE", "lat": -1.29, "lng": 36.82, "pop": "4.7M"},
    {"name": "Pretoria", "country": "ZA", "lat": -25.75, "lng": 28.19, "pop": "2.5M"},
    {"name": "Abuja", "country": "NG", "lat": 9.08, "lng": 7.48, "pop": "3.6M"},
    {"name": "Mexico City", "country": "MX", "lat": 19.43, "lng": -99.13, "pop": "21.8M metro"},
    {"name": "Jakarta", "country": "ID", "lat": -6.21, "lng": 106.85, "pop": "34M metro"},
    {"name": "Bangkok", "country": "TH", "lat": 13.75, "lng": 100.50, "pop": "10.5M"},
    {"name": "Hanoi", "country": "VN", "lat": 21.03, "lng": 105.85, "pop": "8.4M"},
    {"name": "Singapore", "country": "SG", "lat": 1.35, "lng": 103.82, "pop": "5.9M"},
    {"name": "Buenos Aires", "country": "AR", "lat": -34.60, "lng": -58.38, "pop": "15.4M metro"},
    {"name": "Islamabad", "country": "PK", "lat": 33.69, "lng": 73.05, "pop": "1.2M"},
    {"name": "Kabul", "country": "AF", "lat": 34.52, "lng": 69.18, "pop": "4.4M"},
    {"name": "Baghdad", "country": "IQ", "lat": 33.31, "lng": 44.36, "pop": "8.1M"},
    {"name": "Damascus", "country": "SY", "lat": 33.51, "lng": 36.30, "pop": "2.5M"},
    {"name": "Addis Ababa", "country": "ET", "lat": 9.02, "lng": 38.75, "pop": "5.5M"},
]

MAJOR_DATA_CENTERS = [
    {"name": "Ashburn (Data Center Alley)", "country": "US", "lat": 39.04, "lng": -77.49, "operator": "Multiple (AWS/Azure/Equinix)", "detail": "World's largest concentration; 70%+ US internet traffic"},
    {"name": "The Dalles (Google)", "country": "US", "lat": 45.60, "lng": -121.18, "operator": "Google", "detail": "Massive Google campus; hydro-powered"},
    {"name": "Council Bluffs (Google/Meta)", "country": "US", "lat": 41.26, "lng": -95.86, "operator": "Google/Meta", "detail": "Major hyperscale campus"},
    {"name": "Frankfurt Hub", "country": "DE", "lat": 50.11, "lng": 8.68, "operator": "Equinix/DE-CIX", "detail": "Europe's largest internet exchange"},
    {"name": "Amsterdam Hub", "country": "NL", "lat": 52.37, "lng": 4.90, "operator": "Equinix/AMS-IX", "detail": "Major European hub"},
    {"name": "London Slough", "country": "GB", "lat": 51.51, "lng": -0.60, "operator": "Equinix/Digital Realty", "detail": "UK's primary data center cluster"},
    {"name": "Singapore (Tuas)", "country": "SG", "lat": 1.33, "lng": 103.65, "operator": "Multiple", "detail": "SE Asia hub; 60+ facilities"},
    {"name": "Tokyo (Inzai)", "country": "JP", "lat": 35.83, "lng": 140.15, "operator": "AWS/NTT/Equinix", "detail": "Asia's largest cluster"},
    {"name": "Beijing-Zhangbei", "country": "CN", "lat": 41.16, "lng": 114.70, "operator": "Alibaba/Tencent", "detail": "Major Chinese cloud hub"},
    {"name": "Guiyang (Apple/Huawei)", "country": "CN", "lat": 26.65, "lng": 106.63, "operator": "Apple iCloud China/Huawei", "detail": "China's 'Big Data Valley'"},
    {"name": "Mumbai Hub", "country": "IN", "lat": 19.08, "lng": 72.88, "operator": "Multiple", "detail": "India's primary DC cluster"},
    {"name": "São Paulo Hub", "country": "BR", "lat": -23.55, "lng": -46.63, "operator": "Equinix/Ascenty", "detail": "Latin America's largest"},
    {"name": "Sydney Hub", "country": "AU", "lat": -33.87, "lng": 151.21, "operator": "Equinix/NextDC", "detail": "Oceania hub"},
    {"name": "Dubai Hub", "country": "AE", "lat": 25.27, "lng": 55.30, "operator": "Equinix/Gulf Data Hub", "detail": "Middle East hub"},
    {"name": "Hamina (Google)", "country": "FI", "lat": 60.57, "lng": 27.19, "operator": "Google", "detail": "Seawater-cooled; Nordic green power"},
    {"name": "Luleå (Meta)", "country": "SE", "lat": 65.58, "lng": 22.15, "operator": "Meta", "detail": "Arctic cooling; hydro-powered"},
]

UNDERSEA_CABLES = _INFRA_CABLES
OIL_GAS_PIPELINES = _INFRA_PIPELINES
OIL_RARE_EARTH_FIELDS = _INFRA_FIELDS

NUCLEAR_REACTORS = [
    {"name": "Zaporizhzhia NPP", "country": "UA", "lat": 47.51, "lng": 34.59, "capacity_mw": 5700, "status": "occupied", "detail": "Europe's largest NPP; Russian-occupied; IAEA monitoring"},
    {"name": "Hinkley Point C", "country": "GB", "lat": 51.21, "lng": -3.13, "capacity_mw": 3260, "status": "construction", "detail": "First new UK reactor in decades; EPR design; over budget"},
    {"name": "Barakah NPP", "country": "AE", "lat": 23.95, "lng": 52.26, "capacity_mw": 5600, "status": "active", "detail": "First Arab nuclear plant; 4 APR-1400 units"},
    {"name": "Akkuyu NPP", "country": "TR", "lat": 36.14, "lng": 33.53, "capacity_mw": 4800, "status": "construction", "detail": "Rosatom-built; Turkey's first NPP"},
    {"name": "Bushehr NPP", "country": "IR", "lat": 28.83, "lng": 50.89, "capacity_mw": 1000, "status": "active", "detail": "Iran's only power reactor; Russian-supplied VVER-1000"},
    {"name": "Taishan EPR", "country": "CN", "lat": 21.91, "lng": 112.98, "capacity_mw": 3460, "status": "active", "detail": "World's first EPR to operate; 2 units"},
    {"name": "Flamanville 3 EPR", "country": "FR", "lat": 49.54, "lng": -1.88, "capacity_mw": 1630, "status": "active", "detail": "Finally started 2024; 12 years late"},
    {"name": "Vogtle 3&4", "country": "US", "lat": 33.14, "lng": -81.76, "capacity_mw": 2234, "status": "active", "detail": "Only new US reactors in decades; AP1000 design"},
    {"name": "Kudankulam NPP", "country": "IN", "lat": 8.17, "lng": 77.71, "capacity_mw": 4000, "status": "active", "detail": "Russian-supplied VVER-1000; 6 planned units"},
    {"name": "Chernobyl (exclusion zone)", "country": "UA", "lat": 51.39, "lng": 30.10, "capacity_mw": 0, "status": "decommissioned", "detail": "1986 disaster site; New Safe Confinement; occupied then returned"},
    {"name": "Fukushima Daiichi", "country": "JP", "lat": 37.42, "lng": 141.03, "capacity_mw": 0, "status": "decommissioned", "detail": "2011 disaster; treated water release ongoing; decades of cleanup"},
]

# ═══════════════ SHIPPING ROUTES (major global lanes) ═══════════════
SHIPPING_ROUTES = [
    # ── Critical chokepoints ──
    {"name": "Strait of Malacca", "from_loc": {"lat": 1.27, "lng": 103.75, "name": "Singapore"}, "to_loc": {"lat": 6.12, "lng": 100.37, "name": "Penang, Malaysia"}, "traffic": "~94,000 vessels/yr; 25% global trade", "detail": "World's busiest shipping lane; links Indian Ocean to South China Sea; piracy risk"},
    {"name": "Suez Canal", "from_loc": {"lat": 29.95, "lng": 32.56, "name": "Port Said, Egypt"}, "to_loc": {"lat": 29.97, "lng": 32.58, "name": "Suez, Egypt"}, "traffic": "~24,000 transits/yr; 12-15% global trade", "detail": "193 km; Houthi attacks forced rerouting 2024; $10B annual tolls"},
    {"name": "Panama Canal", "from_loc": {"lat": 9.38, "lng": -79.92, "name": "Colon, Panama (Atlantic)"}, "to_loc": {"lat": 8.95, "lng": -79.57, "name": "Panama City (Pacific)"}, "traffic": "~14,000 transits/yr; 5% global trade", "detail": "82 km; drought reduced transits 2023-24; Neopanamax locks since 2016"},
    {"name": "Strait of Hormuz", "from_loc": {"lat": 26.57, "lng": 56.25, "name": "Bandar Abbas, Iran"}, "to_loc": {"lat": 25.84, "lng": 55.95, "name": "Oman/UAE side"}, "traffic": "~21M bpd oil; 20% global oil supply", "detail": "33 km wide; Iran threat to close; most critical oil chokepoint"},
    {"name": "Bab el-Mandeb Strait", "from_loc": {"lat": 12.60, "lng": 43.30, "name": "Djibouti"}, "to_loc": {"lat": 12.80, "lng": 43.50, "name": "Yemen coast"}, "traffic": "~7M bpd oil; 9% global seaborne oil", "detail": "26 km wide; Houthi attacks 2024 disrupted traffic; Red Sea security crisis"},
    {"name": "Strait of Gibraltar", "from_loc": {"lat": 35.90, "lng": -5.35, "name": "Tangier, Morocco"}, "to_loc": {"lat": 36.14, "lng": -5.35, "name": "Gibraltar/Algeciras"}, "traffic": "~76,000 vessels/yr", "detail": "14 km wide; gateway between Atlantic and Mediterranean"},
    {"name": "Bosphorus & Dardanelles (Turkish Straits)", "from_loc": {"lat": 41.22, "lng": 29.05, "name": "Istanbul, Bosphorus"}, "to_loc": {"lat": 40.07, "lng": 26.37, "name": "Canakkale, Dardanelles"}, "traffic": "~42,000 vessels/yr", "detail": "Russia/Black Sea access to Mediterranean; Montreux Convention; mine risk in wartime"},
    {"name": "Danish Straits (Oresund/Great Belt)", "from_loc": {"lat": 55.68, "lng": 12.60, "name": "Copenhagen, Denmark"}, "to_loc": {"lat": 56.15, "lng": 10.22, "name": "Aarhus, Denmark"}, "traffic": "~60,000 vessels/yr", "detail": "Baltic Sea access; critical for Russian/Nordic trade; NATO choke point"},
    {"name": "Taiwan Strait", "from_loc": {"lat": 24.45, "lng": 118.08, "name": "Xiamen, China"}, "to_loc": {"lat": 25.05, "lng": 121.53, "name": "Taipei, Taiwan"}, "traffic": "~88% of largest container ships transit", "detail": "180 km wide; geopolitical flashpoint; semiconductor supply chain risk"},
    {"name": "English Channel", "from_loc": {"lat": 50.92, "lng": 1.85, "name": "Calais, France"}, "to_loc": {"lat": 51.13, "lng": 1.32, "name": "Dover, UK"}, "traffic": "~500 vessels/day", "detail": "33 km at narrowest; world's busiest international seaway; Dover Strait TSS"},
    # ── Major oceanic routes ──
    {"name": "Cape of Good Hope Route", "from_loc": {"lat": -34.36, "lng": 18.47, "name": "Cape Town, South Africa"}, "to_loc": {"lat": -33.86, "lng": 25.57, "name": "Port Elizabeth, South Africa"}, "traffic": "Alternative to Suez; surge in 2024", "detail": "Adds 10-14 days vs Suez Canal; rerouted traffic from Red Sea crisis"},
    {"name": "North Atlantic Route (Transatlantic)", "from_loc": {"lat": 40.67, "lng": -74.03, "name": "New York/New Jersey"}, "to_loc": {"lat": 51.95, "lng": 4.05, "name": "Rotterdam, Netherlands"}, "traffic": "~$500B/yr bilateral trade", "detail": "World's most established trade lane; container and bulk cargo"},
    {"name": "Trans-Pacific Route (Asia-North America)", "from_loc": {"lat": 31.23, "lng": 121.47, "name": "Shanghai, China"}, "to_loc": {"lat": 33.74, "lng": -118.27, "name": "Los Angeles/Long Beach"}, "traffic": "~$1T/yr trade value", "detail": "World's highest-value shipping lane; 10,000+ km; 12-16 day transit"},
    {"name": "South China Sea Route", "from_loc": {"lat": 1.27, "lng": 103.75, "name": "Singapore"}, "to_loc": {"lat": 22.25, "lng": 114.17, "name": "Hong Kong"}, "traffic": "~$5.3T/yr in trade passes through", "detail": "Disputed territorial waters; Chinese military buildup; 9-dash line claims"},
    {"name": "Cape Horn Route", "from_loc": {"lat": -55.98, "lng": -67.27, "name": "Cape Horn, Chile"}, "to_loc": {"lat": -34.61, "lng": -58.38, "name": "Buenos Aires, Argentina"}, "traffic": "Backup for Panama Canal", "detail": "Treacherous waters; used for oversized vessels and Panama alternatives"},
    {"name": "Mozambique Channel", "from_loc": {"lat": -11.85, "lng": 43.87, "name": "Comoros"}, "to_loc": {"lat": -25.97, "lng": 32.57, "name": "Maputo, Mozambique"}, "traffic": "Major oil tanker route", "detail": "460 km wide; LNG tankers from Mozambique fields; cyclone risk"},
    {"name": "Lombok Strait", "from_loc": {"lat": -8.39, "lng": 115.69, "name": "Bali, Indonesia"}, "to_loc": {"lat": -8.57, "lng": 116.35, "name": "Lombok, Indonesia"}, "traffic": "Alternative to Malacca for large vessels", "detail": "Deeper draft than Malacca; used by VLCCs and Capesize bulkers"},
    {"name": "Northern Sea Route (Arctic)", "from_loc": {"lat": 69.35, "lng": 33.08, "name": "Murmansk, Russia"}, "to_loc": {"lat": 43.12, "lng": 131.87, "name": "Vladivostok, Russia"}, "traffic": "~2,000 transits/yr; growing", "detail": "Russia controls; 40% shorter than Suez for Europe-Asia; seasonal ice"},
    {"name": "Mediterranean-Black Sea Route", "from_loc": {"lat": 37.95, "lng": 23.72, "name": "Piraeus, Greece"}, "to_loc": {"lat": 41.01, "lng": 28.98, "name": "Istanbul, Turkey"}, "traffic": "~65,000 vessels/yr", "detail": "Critical for grain exports from Ukraine/Russia; Turkish Straits bottleneck"},
    {"name": "Gulf of Aden Route", "from_loc": {"lat": 11.59, "lng": 43.15, "name": "Djibouti"}, "to_loc": {"lat": 12.78, "lng": 45.04, "name": "Aden, Yemen"}, "traffic": "Gateway to Red Sea/Suez", "detail": "Counter-piracy naval patrols; Houthi missile/drone threat zone since 2023"},
    {"name": "East Africa Coastal Route", "from_loc": {"lat": -4.05, "lng": 39.67, "name": "Mombasa, Kenya"}, "to_loc": {"lat": -33.92, "lng": 18.42, "name": "Cape Town, South Africa"}, "traffic": "Regional trade corridor", "detail": "Container and bulk cargo linking East African ports to Southern Africa and Asia"},
]

# ═══════════════ INDUSTRIAL CENTERS (major manufacturing hubs) ═══════════════
INDUSTRIAL_CENTERS = [
    # ── China ──
    {"name": "Shenzhen", "country": "CN", "lat": 22.54, "lng": 114.06, "type": "electronics", "detail": "World's electronics manufacturing hub; Foxconn, Huawei, ZTE, BYD HQs"},
    {"name": "Dongguan", "country": "CN", "lat": 23.02, "lng": 113.75, "type": "electronics", "detail": "Pearl River Delta factory city; shoes, toys, electronics for global brands"},
    {"name": "Shanghai Pudong", "country": "CN", "lat": 31.22, "lng": 121.54, "type": "mixed", "detail": "Financial center + advanced manufacturing; SMIC semiconductor fabs; Tesla Gigafactory"},
    {"name": "Guangzhou", "country": "CN", "lat": 23.13, "lng": 113.26, "type": "automotive", "detail": "Major auto hub; Toyota, Honda, GAC factories; Pearl River Delta logistics center"},
    {"name": "Suzhou Industrial Park", "country": "CN", "lat": 31.30, "lng": 120.59, "type": "mixed", "detail": "China-Singapore joint venture zone; biotech, nanotech, semiconductors"},
    {"name": "Chongqing", "country": "CN", "lat": 29.56, "lng": 106.55, "type": "automotive", "detail": "China's largest auto production base; start of Belt & Road rail freight to Europe"},
    # ── Japan / South Korea / Taiwan ──
    {"name": "Toyota City", "country": "JP", "lat": 35.08, "lng": 137.16, "type": "automotive", "detail": "Global HQ of Toyota Motor; world's largest automaker by volume"},
    {"name": "Ulsan", "country": "KR", "lat": 35.54, "lng": 129.31, "type": "automotive/shipbuilding", "detail": "Hyundai Motor + world's largest shipyard (Hyundai Heavy Industries)"},
    {"name": "Hsinchu Science Park", "country": "TW", "lat": 24.78, "lng": 120.99, "type": "semiconductors", "detail": "TSMC HQ; world's most advanced chip fabrication; 90% of cutting-edge nodes"},
    {"name": "Samsung Pyeongtaek Campus", "country": "KR", "lat": 36.99, "lng": 127.09, "type": "semiconductors", "detail": "World's largest semiconductor fab complex; $230B+ invested"},
    # ── United States ──
    {"name": "Detroit Metro", "country": "US", "lat": 42.33, "lng": -83.05, "type": "automotive", "detail": "Big Three HQ (GM, Ford, Stellantis); legacy auto + EV transition hub"},
    {"name": "Silicon Valley", "country": "US", "lat": 37.39, "lng": -122.08, "type": "technology", "detail": "Global tech innovation center; Apple, Google, Meta, NVIDIA HQs"},
    {"name": "Houston Ship Channel", "country": "US", "lat": 29.73, "lng": -95.22, "type": "petrochemical", "detail": "World's largest petrochemical complex; 600+ chemical plants and refineries"},
    {"name": "Austin/San Antonio Corridor", "country": "US", "lat": 30.27, "lng": -97.74, "type": "semiconductors", "detail": "Samsung, NXP, Tesla Gigafactory Texas; CHIPS Act investment hub"},
    # ── Europe ──
    {"name": "Stuttgart", "country": "DE", "lat": 48.78, "lng": 9.18, "type": "automotive", "detail": "Mercedes-Benz, Porsche HQs; Bosch; precision engineering cluster"},
    {"name": "Wolfsburg", "country": "DE", "lat": 52.42, "lng": 10.78, "type": "automotive", "detail": "Volkswagen global HQ; Europe's largest car factory; 60,000 employees"},
    {"name": "Ruhr Area (Essen-Dortmund)", "country": "DE", "lat": 51.45, "lng": 7.01, "type": "heavy_industry", "detail": "Europe's largest industrial area; steel, chemicals, energy; ThyssenKrupp"},
    {"name": "Rotterdam-Antwerp Petrochemical Cluster", "country": "NL/BE", "lat": 51.90, "lng": 4.48, "type": "petrochemical", "detail": "Europe's largest port + refining complex; Shell, BASF, ExxonMobil plants"},
    {"name": "Toulouse Aerospace Cluster", "country": "FR", "lat": 43.60, "lng": 1.44, "type": "aerospace", "detail": "Airbus HQ; A320/A350/A380 final assembly; 120,000 aerospace jobs"},
    # ── South / Southeast Asia ──
    {"name": "Dhaka-Gazipur Garment District", "country": "BD", "lat": 23.99, "lng": 90.42, "type": "textiles", "detail": "World's 2nd-largest garment exporter; $42B/yr; 4M+ workers; Rana Plaza legacy"},
    {"name": "Bangalore (Bengaluru)", "country": "IN", "lat": 12.97, "lng": 77.59, "type": "IT/software", "detail": "India's Silicon Valley; Infosys, Wipro, TCS; $150B+ IT exports"},
    {"name": "Batam Industrial Zone", "country": "ID", "lat": 1.05, "lng": 104.03, "type": "electronics", "detail": "Free trade zone near Singapore; electronics assembly; shipbuilding"},
    # ── Middle East ──
    {"name": "Jubail Industrial City", "country": "SA", "lat": 27.01, "lng": 49.62, "type": "petrochemical", "detail": "World's largest industrial city; SABIC, Saudi Aramco; $100B+ invested"},
    {"name": "Ras Laffan Industrial City", "country": "QA", "lat": 25.92, "lng": 51.53, "type": "LNG", "detail": "World's largest LNG production hub; QatarEnergy; North Field expansion"},
    {"name": "Jamnagar Refinery Complex", "country": "IN", "lat": 22.47, "lng": 70.06, "type": "refining", "detail": "World's largest oil refinery; 1.4M bpd; Reliance Industries; exports globally"},
]

# ═══════════════ ECONOMIC ZONES (SEZs, FTZs, special economic zones) ═══════════════
ECONOMIC_ZONES = [
    # ── China ──
    {"name": "Shenzhen SEZ", "country": "CN", "lat": 22.54, "lng": 114.06, "type": "sez", "detail": "China's first and most successful SEZ (1980); $450B GDP; tech innovation hub"},
    {"name": "Shanghai Pilot Free Trade Zone", "country": "CN", "lat": 31.32, "lng": 121.60, "type": "ftz", "detail": "China's first FTZ (2013); Lingang expansion; financial liberalization testbed"},
    {"name": "Hainan Free Trade Port", "country": "CN", "lat": 20.02, "lng": 110.35, "type": "ftp", "detail": "Entire island as FTP by 2025; zero tariffs; China's answer to Hong Kong/Singapore"},
    {"name": "Xiamen SEZ", "country": "CN", "lat": 24.48, "lng": 118.09, "type": "sez", "detail": "One of original 1980 SEZs; Taiwan-facing trade hub; electronics and services"},
    # ── Middle East ──
    {"name": "Jebel Ali Free Zone (JAFZA)", "country": "AE", "lat": 24.99, "lng": 55.06, "type": "ftz", "detail": "World's largest FTZ; 8,700+ companies; adjacent to world's largest man-made port"},
    {"name": "Dubai International Financial Centre (DIFC)", "country": "AE", "lat": 25.21, "lng": 55.28, "type": "financial", "detail": "Common-law financial center within UAE; 4,000+ firms; $5T capital flows"},
    {"name": "Aqaba SEZ", "country": "JO", "lat": 29.53, "lng": 35.01, "type": "sez", "detail": "Jordan's only coastal city; 0% income tax zone; Red Sea logistics hub"},
    {"name": "NEOM (planned)", "country": "SA", "lat": 28.00, "lng": 35.20, "type": "megaproject", "detail": "Saudi Vision 2030; $500B planned; The Line, Trojena; green hydrogen ambitions"},
    # ── Southeast Asia ──
    {"name": "Singapore (entire territory)", "country": "SG", "lat": 1.35, "lng": 103.82, "type": "ftp", "detail": "Entire country is effectively an FTZ; 0% tariff on most goods; global trade hub"},
    {"name": "Batam Free Trade Zone", "country": "ID", "lat": 1.05, "lng": 104.03, "type": "ftz", "detail": "Indonesia's largest FTZ; near Singapore; electronics, shipbuilding, tourism"},
    {"name": "Subic Bay Freeport Zone", "country": "PH", "lat": 14.82, "lng": 120.28, "type": "freeport", "detail": "Former US naval base; duty-free zone; logistics, shipbuilding, tourism"},
    {"name": "Iskandar Malaysia (Johor)", "country": "MY", "lat": 1.55, "lng": 103.73, "type": "sez", "detail": "Bordering Singapore; 3x size of Singapore; data centers, petrochemicals, logistics"},
    # ── India ──
    {"name": "Gujarat International Finance Tec-City (GIFT)", "country": "IN", "lat": 23.11, "lng": 72.58, "type": "financial", "detail": "India's first IFSC; modeled on Singapore/Dubai; fintech, bullion trading"},
    {"name": "SEEPZ Mumbai", "country": "IN", "lat": 19.15, "lng": 72.87, "type": "sez", "detail": "India's oldest export zone (1973); gems, jewelry, electronics; $8B/yr exports"},
    # ── Europe / Americas / Other ──
    {"name": "Shannon Free Zone", "country": "IE", "lat": 52.70, "lng": -8.85, "type": "ftz", "detail": "World's first free trade zone (1959); aviation, pharma, tech; model for global FTZs"},
    {"name": "Manaus Free Trade Zone", "country": "BR", "lat": -3.12, "lng": -60.02, "type": "ftz", "detail": "Amazon rainforest electronics hub; Samsung, Honda; tax incentives to deter deforestation"},
    {"name": "Colon Free Trade Zone", "country": "PA", "lat": 9.35, "lng": -79.90, "type": "ftz", "detail": "Hemisphere's largest FTZ; adjacent to Panama Canal; $14B/yr re-export trade"},
    {"name": "Shenzhen Qianhai Cooperation Zone", "country": "CN", "lat": 22.52, "lng": 113.90, "type": "ftz", "detail": "HK-Shenzhen cooperation zone; fintech, legal services; 15% corporate tax"},
]



# ═══════════════ SPACEPORTS & LAUNCH FACILITIES ═══════════════
SPACEPORTS = [
    # ── United States ──
    {"name": "Kennedy Space Center (LC-39)", "country": "US", "lat": 28.57, "lng": -80.65, "operator": "NASA/SpaceX", "status": "active", "detail": "Crewed launches; Artemis SLS; SpaceX Falcon/Starship; LC-39A/B"},
    {"name": "Cape Canaveral SFS (SLC-40/41)", "country": "US", "lat": 28.49, "lng": -80.58, "operator": "SpaceX/ULA", "status": "active", "detail": "Falcon 9 (SLC-40); Atlas V/Vulcan (SLC-41); highest launch cadence globally"},
    {"name": "Vandenberg SFB", "country": "US", "lat": 34.75, "lng": -120.52, "operator": "USSF/SpaceX/ULA", "status": "active", "detail": "Polar/SSO launches; Falcon 9, Delta IV Heavy; NRO payloads"},
    {"name": "SpaceX Starbase (Boca Chica)", "country": "US", "lat": 25.99, "lng": -97.15, "operator": "SpaceX", "status": "active", "detail": "Starship/Super Heavy development & launch site; orbital launch pad"},
    {"name": "Wallops Flight Facility", "country": "US", "lat": 37.93, "lng": -75.47, "operator": "NASA/MARS", "status": "active", "detail": "Antares/Minotaur launches; ISS resupply (Cygnus); sounding rockets"},
    {"name": "Kodiak Launch Complex", "country": "US", "lat": 57.44, "lng": -152.34, "operator": "AASC", "status": "active", "detail": "Polar launches from Alaska; Astra, ABL Space"},
    {"name": "Mojave Air & Space Port", "country": "US", "lat": 35.06, "lng": -118.15, "operator": "Various", "status": "active", "detail": "Horizontal launch; Virgin Orbit (ceased); Scaled Composites; test flights"},
    {"name": "Cecil Spaceport", "country": "US", "lat": 30.22, "lng": -81.88, "operator": "JAA", "status": "active", "detail": "Horizontal launch site; Jacksonville, FL; small sat launchers"},
    {"name": "Stennis Space Center", "country": "US", "lat": 30.37, "lng": -89.60, "operator": "NASA", "status": "active", "detail": "Rocket engine test facility; SLS core stage testing; Mississippi"},
    # ── Russia ──
    {"name": "Baikonur Cosmodrome", "country": "KZ", "lat": 45.97, "lng": 63.31, "operator": "Roscosmos (leased from KZ)", "status": "active", "detail": "World's first spaceport (1957); Soyuz, Proton launches; ISS crew launches"},
    {"name": "Plesetsk Cosmodrome", "country": "RU", "lat": 62.93, "lng": 40.58, "operator": "Russian MoD", "status": "active", "detail": "Military launches; Soyuz-2, Angara; ICBM testing; world's busiest by total launches"},
    {"name": "Vostochny Cosmodrome", "country": "RU", "lat": 51.88, "lng": 128.33, "operator": "Roscosmos", "status": "active", "detail": "Russia's newest spaceport (2016); replacing Baikonur dependence; Soyuz-2, Angara-A5"},
    {"name": "Kapustin Yar", "country": "RU", "lat": 48.58, "lng": 45.77, "operator": "Russian MoD", "status": "active", "detail": "Missile test range; sounding rockets; Soviet-era origins (1946)"},
    # ── China ──
    {"name": "Jiuquan Satellite Launch Center", "country": "CN", "lat": 40.96, "lng": 100.29, "operator": "CNSA/PLA", "status": "active", "detail": "Crewed Shenzhou launches; Long March 2F; Gobi Desert; China's first spaceport"},
    {"name": "Xichang Satellite Launch Center", "country": "CN", "lat": 28.25, "lng": 102.03, "operator": "CNSA", "status": "active", "detail": "GEO/BeiDou launches; Long March 3B; Sichuan province"},
    {"name": "Taiyuan Satellite Launch Center", "country": "CN", "lat": 38.85, "lng": 111.61, "operator": "CNSA", "status": "active", "detail": "Polar/SSO launches; Long March 4/6; Shanxi province"},
    {"name": "Wenchang Space Launch Site", "country": "CN", "lat": 19.61, "lng": 110.95, "operator": "CNSA", "status": "active", "detail": "Heavy-lift launches; Long March 5/7/8; Hainan; China's newest & most modern"},
    {"name": "Eastern China Sea Launch (mobile)", "country": "CN", "lat": 34.50, "lng": 121.00, "operator": "CASIC", "status": "active", "detail": "Sea-based launches from converted ships; Long March 11; flexible launch location"},
    # ── Europe ──
    {"name": "Guiana Space Centre (Kourou)", "country": "FR", "lat": 5.24, "lng": -52.77, "operator": "ESA/Arianespace/CNES", "status": "active", "detail": "Ariane 6, Vega-C, Soyuz (suspended); near equator; French Guiana"},
    {"name": "Esrange Space Center", "country": "SE", "lat": 67.89, "lng": 21.10, "operator": "SSC", "status": "active", "detail": "Sounding rockets; small sat orbital launches planned; Kiruna, Sweden"},
    {"name": "Andøya Spaceport", "country": "NO", "lat": 69.29, "lng": 16.02, "operator": "ASP", "status": "development", "detail": "Planned small-sat polar orbit launches; first European continental orbital pad"},
    {"name": "SaxaVord Spaceport", "country": "GB", "lat": 60.82, "lng": -0.78, "operator": "SaxaVord UK", "status": "development", "detail": "UK's first vertical launch site; Shetland Islands; polar/SSO orbits"},
    {"name": "Sutherland Spaceport", "country": "GB", "lat": 58.51, "lng": -5.05, "operator": "Orbex/HIE", "status": "development", "detail": "Scottish Highlands; Orbex Prime launcher; planned 2025+"},
    # ── India ──
    {"name": "Satish Dhawan Space Centre (SHAR)", "country": "IN", "lat": 13.72, "lng": 80.23, "operator": "ISRO", "status": "active", "detail": "GSLV Mk III/LVM3, PSLV launches; Sriharikota island; Chandrayaan/Gaganyaan"},
    {"name": "Thumba Equatorial Rocket Launching Station", "country": "IN", "lat": 8.53, "lng": 76.87, "operator": "ISRO", "status": "active", "detail": "Sounding rockets; near magnetic equator; India's first launch site (1963)"},
    {"name": "Kulasekarapattinam (planned)", "country": "IN", "lat": 8.58, "lng": 78.09, "operator": "ISRO", "status": "development", "detail": "Planned SSLV launch pad; Tamil Nadu; small satellite launches"},
    # ── Japan ──
    {"name": "Tanegashima Space Center", "country": "JP", "lat": 30.40, "lng": 131.00, "operator": "JAXA", "status": "active", "detail": "H3, H-IIA launches; Japan's primary orbital launch site; Kagoshima prefecture"},
    {"name": "Uchinoura Space Center", "country": "JP", "lat": 31.25, "lng": 131.08, "operator": "JAXA", "status": "active", "detail": "Epsilon rocket; scientific satellites; sounding rockets"},
    # ── South Korea ──
    {"name": "Naro Space Center", "country": "KR", "lat": 34.43, "lng": 127.54, "operator": "KARI/KASA", "status": "active", "detail": "Nuri (KSLV-II) launches; South Korea's only launch site; Goheung"},
    # ── New Zealand ──
    {"name": "Rocket Lab Launch Complex 1", "country": "NZ", "lat": -39.26, "lng": 177.86, "operator": "Rocket Lab", "status": "active", "detail": "Electron rocket; high-cadence small-sat launches; Mahia Peninsula"},
    # ── Middle East ──
    {"name": "Imam Khomeini Space Center (Semnan)", "country": "IR", "lat": 35.23, "lng": 53.92, "operator": "ISA/IRGC", "status": "active", "detail": "Simorgh, Qaem-100 SLV launches; dual-use ICBM concern; Semnan province"},
    {"name": "Shahroud Missile Complex", "country": "IR", "lat": 36.42, "lng": 55.02, "operator": "IRGC-ASF", "status": "active", "detail": "Military space/missile launches; solid-fuel SLVs; less monitored than Semnan"},
    {"name": "Palmachim Airbase", "country": "IL", "lat": 31.88, "lng": 34.69, "operator": "IAI/ISA", "status": "active", "detail": "Shavit SLV launches (retrograde orbit to avoid overflying neighbors); Ofeq satellites"},
    # ── North Korea ──
    {"name": "Sohae Satellite Launching Station", "country": "KP", "lat": 39.66, "lng": 124.71, "operator": "NADA", "status": "active", "detail": "Unha/Chollima SLV launches; dual-use ICBM technology; Dongchang-ri"},
    {"name": "Tonghae Satellite Launching Ground", "country": "KP", "lat": 40.85, "lng": 129.67, "operator": "NADA", "status": "active", "detail": "East coast launch site; Musudan-ri; ballistic missile tests"},
    # ── South America ──
    {"name": "Alcântara Launch Center", "country": "BR", "lat": -2.37, "lng": -44.40, "operator": "FAB/AEB", "status": "active", "detail": "Near-equatorial (2°S); most fuel-efficient GEO launches; VLS rockets; commercial expansion"},
    {"name": "Barreira do Inferno Launch Center", "country": "BR", "lat": -5.92, "lng": -35.26, "operator": "FAB", "status": "active", "detail": "Sounding rockets; Brazil's first launch site (1965); Natal, RN"},
    # ── Australia ──
    {"name": "Woomera Test Range", "country": "AU", "lat": -31.16, "lng": 136.83, "operator": "ADF/RAAF", "status": "active", "detail": "Missile/rocket testing; vast restricted area; South Australia; hypersonics testing"},
    {"name": "Arnhem Space Centre", "country": "AU", "lat": -12.45, "lng": 136.80, "operator": "ELA", "status": "active", "detail": "Near-equatorial launch; NASA sounding rockets; commercial small-sat launches planned"},
    {"name": "Bowen Orbital Launch Complex", "country": "AU", "lat": -20.00, "lng": 148.20, "operator": "Gilmour Space", "status": "development", "detail": "Queensland; Eris rocket; Australian commercial orbital launches"},
    # ── Pakistan ──
    {"name": "Tilla Satellite Launch Center", "country": "PK", "lat": 32.93, "lng": 73.35, "operator": "SUPARCO", "status": "limited", "detail": "Sounding rockets; Pakistan's primary space launch research facility"},
    {"name": "Sonmiani Rocket Range", "country": "PK", "lat": 25.25, "lng": 66.75, "operator": "SUPARCO", "status": "active", "detail": "Sounding rocket launches; Balochistan coast; atmospheric research"},
    # ── Other ──
    {"name": "San Marco Platform", "country": "IT", "lat": -2.94, "lng": 40.21, "operator": "ASI (inactive)", "status": "retired", "detail": "Ocean-based platform off Kenya coast; Italian launches (1967-1988); equatorial"},
    {"name": "Hammaguir Launch Site", "country": "DZ", "lat": 31.00, "lng": -3.05, "operator": "CNES (former)", "status": "retired", "detail": "French Saharan launch site; Diamant A/B (1965-1967); returned to Algeria"},
    {"name": "Rocket Lab Launch Complex 2", "country": "US", "lat": 37.84, "lng": -75.49, "operator": "Rocket Lab", "status": "active", "detail": "Electron & Neutron rockets; Wallops Island, Virginia; US-based launches"},
    {"name": "Jiuquan Commercial Launch Zone", "country": "CN", "lat": 40.90, "lng": 100.31, "operator": "LandSpace/iSpace", "status": "active", "detail": "Chinese commercial launch companies; Zhuque-2, Hyperbola; private sector space"},
]


# ═══════════════ WORLD ARMIES (GROUND FORCES) ═══════════════
# Active personnel + reserves + key equipment for all nations with standing armies
# Sources: IISS Military Balance, SIPRI, GlobalFirepower, national MoDs (open sources)
WORLD_ARMIES = [
    # ── Top 20 by personnel/capability ──
    {"country": "CN", "name": "People's Liberation Army Ground Force (PLAGF)", "active": 965000, "reserves": 510000, "paramilitary": 660000, "tanks": 4800, "ifv_apc": 8200, "artillery": 9700, "mlrs": 3050, "key_equipment": "Type 99A2, Type 96B, ZTQ-15, ZBD-04A, PLZ-05, PHL-16, DF-21D/26, HQ-9", "detail": "World's largest ground force; rapid mechanization; strategic rocket force separate (PLARF)"},
    {"country": "IN", "name": "Indian Army", "active": 1237000, "reserves": 960000, "paramilitary": 1585000, "tanks": 4614, "ifv_apc": 8686, "artillery": 4060, "mlrs": 264, "key_equipment": "T-90S Bhishma, T-72M1, Arjun Mk1A, BMP-2 Sarath, K9 Vajra, Pinaka, Dhanush, BrahMos", "detail": "Second-largest standing army; Himalayan/Pakistan border; nuclear capable"},
    {"country": "RU", "name": "Russian Ground Forces (incl. Airborne, Naval Inf)", "active": 550000, "reserves": 1500000, "paramilitary": 554000, "tanks": 12420, "ifv_apc": 30122, "artillery": 14774, "mlrs": 3391, "key_equipment": "T-90M, T-80BVM, T-72B3M, T-14 Armata (limited), BMP-3, BMD-4M, 2S19 Msta, BM-30 Smerch, Iskander-M", "detail": "Heavy attrition in Ukraine; massive reserves; large tank stockpile (most still Soviet)"},
    {"country": "KP", "name": "Korean People's Army Ground Force", "active": 950000, "reserves": 600000, "paramilitary": 5700000, "tanks": 6045, "ifv_apc": 2500, "artillery": 21100, "mlrs": 5500, "key_equipment": "Pokpung-ho, Chonma-ho, Songun-ho, T-62, BTR-80A, M1989 Koksan, KN-25 600mm MLRS, Hwasong-17", "detail": "Massive but technologically dated; world's largest artillery park; nuclear capable"},
    {"country": "US", "name": "US Army + USMC Ground", "active": 624000, "reserves": 522000, "paramilitary": 0, "tanks": 4640, "ifv_apc": 13980, "artillery": 1339, "mlrs": 698, "key_equipment": "M1A2 SEPv3 Abrams, M2A4 Bradley, Stryker, AMPV, M109A7 Paladin, M270 MLRS, HIMARS, Patriot, THAAD", "detail": "Most technologically advanced; global power projection; AMPV replacing M113"},
    {"country": "PK", "name": "Pakistan Army", "active": 560000, "reserves": 550000, "paramilitary": 291000, "tanks": 3742, "ifv_apc": 2828, "artillery": 4619, "mlrs": 600, "key_equipment": "VT-4 (Haider), Al-Khalid, T-80UD, Type 85, M113, A100E MLRS, Nasr (Hatf-IX), Shaheen-III", "detail": "Nuclear capable; FATA counter-insurgency; rapid Chinese modernization"},
    {"country": "KR", "name": "Republic of Korea Army (ROKA)", "active": 420000, "reserves": 3100000, "paramilitary": 0, "tanks": 2200, "ifv_apc": 3200, "artillery": 5959, "mlrs": 575, "key_equipment": "K2 Black Panther, K1A2, K21 IFV, K9 Thunder SPH, K239 Chunmoo MLRS, Hyunmoo-2/4 SRBM", "detail": "Best-armed peninsula force; conscript-based; Chunmoo HIMARS-class; counter-NK posture"},
    {"country": "VN", "name": "People's Army of Vietnam (VPA Ground)", "active": 412000, "reserves": 5040000, "paramilitary": 40000, "tanks": 2155, "ifv_apc": 2700, "artillery": 3370, "mlrs": 950, "key_equipment": "T-90S/SK, T-62, T-54/55 (modernized), BMP-1/2, 2S3 Akatsiya, EXTRA, S-300PMU1, Bastion-P", "detail": "Massive reserve mobilization; T-90 modernization; coastal defense focused"},
    {"country": "IR", "name": "Iranian Army (Artesh) + IRGC Ground Force", "active": 610000, "reserves": 350000, "paramilitary": 220000, "tanks": 1996, "ifv_apc": 1380, "artillery": 6798, "mlrs": 1900, "key_equipment": "Karrar, Zulfiqar-3, T-72S, BMP-2 (Boragh), Fajr-5, Naze'at, Fateh-110/313, Zelzal-2/3, Shahed-136", "detail": "Aging armor; massive missile/rocket force; proxy operations; Basij volunteers"},
    {"country": "EG", "name": "Egyptian Army", "active": 310000, "reserves": 480000, "paramilitary": 397000, "tanks": 4694, "ifv_apc": 4500, "artillery": 4480, "mlrs": 1500, "key_equipment": "M1A1 Abrams (locally assembled), M60A3, T-80U, K9 Thunder (planned), M109A5, BM-21 Grad, Sakr", "detail": "Africa's largest mechanized force; Sinai counter-insurgency; mixed US/Russian/Korean"},
    {"country": "TR", "name": "Turkish Army (Türk Kara Kuvvetleri)", "active": 260200, "reserves": 378700, "paramilitary": 156800, "tanks": 2238, "ifv_apc": 8456, "artillery": 2800, "mlrs": 538, "key_equipment": "Altay (entering), Leopard 2A4, M60T Sabra, M48A5, ACV-15, Kaplan-20, T-155 Fırtına, T-300 Kasırga, Bayraktar TB2", "detail": "Indigenous Altay MBT; PKK/Syria operations; drone integration; NATO 2nd-largest"},
    {"country": "SY", "name": "Syrian Arab Army (legacy + post-Assad fragmented)", "active": 130000, "reserves": 100000, "paramilitary": 50000, "tanks": 2700, "ifv_apc": 2000, "artillery": 3050, "mlrs": 500, "key_equipment": "T-72/72M1, T-90A (Russian-supplied), BMP-1/2, 2S1, BM-21, Tochka-U", "detail": "Severely degraded; post-Assad fragmentation; heavy attrition"},
    {"country": "TW", "name": "Republic of China Army (ROCA)", "active": 88000, "reserves": 1657000, "paramilitary": 11800, "tanks": 1100, "ifv_apc": 1300, "artillery": 1800, "mlrs": 305, "key_equipment": "M1A2T Abrams (delivering), CM-11/12, M60A3 TTS, CM-32 Yunpao, M109A6, Thunderbolt-2000 MLRS", "detail": "Conscript reserve mobilization; M1A2T from US; cross-strait deterrence"},
    {"country": "BR", "name": "Brazilian Army (Exército Brasileiro)", "active": 219000, "reserves": 1340000, "paramilitary": 395000, "tanks": 469, "ifv_apc": 2168, "artillery": 1830, "mlrs": 75, "key_equipment": "Leopard 1A5BR, M60A3 TTS, M113BR, Guarani 6×6, M109A5, ASTROS II MLRS", "detail": "Largest in Latin America; Amazon/border patrol; ASTROS exports"},
    {"country": "FR", "name": "French Army (Armée de Terre)", "active": 118600, "reserves": 31000, "paramilitary": 105000, "tanks": 222, "ifv_apc": 6322, "artillery": 109, "mlrs": 13, "key_equipment": "Leclerc XLR, VBCI, Griffon, Jaguar, Serval, CAESAR 155mm SPH, LRU MLRS", "detail": "Scorpion modernization; expeditionary; Sahel/Mali (withdrawn); CAESAR exported"},
    {"country": "DE", "name": "German Army (Deutsches Heer)", "active": 62500, "reserves": 30050, "paramilitary": 0, "tanks": 296, "ifv_apc": 2156, "artillery": 121, "mlrs": 26, "key_equipment": "Leopard 2A6/A7V, Puma IFV, Boxer, Marder (retiring), PzH 2000, MARS II MLRS", "detail": "Zeitenwende €100B special fund; Puma reliability issues; Leopard 2 to Ukraine"},
    {"country": "GB", "name": "British Army", "active": 76000, "reserves": 31000, "paramilitary": 0, "tanks": 213, "ifv_apc": 4750, "artillery": 89, "mlrs": 35, "key_equipment": "Challenger 2/3 (upgrading), Warrior IFV, Boxer (replacing), AS-90, M270 MLRS, Apache AH-64E", "detail": "Smallest in centuries; Challenger 3 upgrade; Boxer/Ajax acquisition"},
    {"country": "IT", "name": "Italian Army (Esercito Italiano)", "active": 96400, "reserves": 18300, "paramilitary": 175750, "tanks": 200, "ifv_apc": 3145, "artillery": 220, "mlrs": 21, "key_equipment": "Ariete (limited), Leopard 2A8 (ordered), Centauro II, Dardo IFV, Freccia, PzH 2000, M270", "detail": "Carabinieri paramilitary largest; Ariete replacement Leopard 2A8; Centauro II"},
    {"country": "PL", "name": "Polish Land Forces (Wojska Lądowe)", "active": 122000, "reserves": 35000, "paramilitary": 16000, "tanks": 614, "ifv_apc": 1600, "artillery": 884, "mlrs": 220, "key_equipment": "K2 Black Panther (PL), M1A2 SEPv3, Leopard 2PL, T-72M1R, Borsuk IFV, K9, HIMARS, Krab SPH", "detail": "Largest NATO eastern flank force; massive K2/M1A2 buy; HIMARS expansion"},
    {"country": "JP", "name": "Japan Ground Self-Defense Force (JGSDF)", "active": 150000, "reserves": 56000, "paramilitary": 12650, "tanks": 524, "ifv_apc": 1077, "artillery": 543, "mlrs": 99, "key_equipment": "Type 10, Type 90, Type 16 MCV, Type 89 IFV, Type 99 SPH, Type 19 wheeled SPH, Type 12 SSM", "detail": "Type 12 SSM with 1500km extended range planned; islands defense; UH-2 utility heli"},

    # ── Major NATO/EU ──
    {"country": "ES", "name": "Spanish Army (Ejército de Tierra)", "active": 79075, "reserves": 14600, "paramilitary": 75800, "tanks": 327, "ifv_apc": 2340, "artillery": 222, "mlrs": 14, "key_equipment": "Leopard 2A4/2E, Pizarro IFV, BMR, M109A5, MLRS, NH90, Tigre", "detail": "Leopard 2E indigenous variant; Sahel/Iraq deployments; Guardia Civil paramilitary"},
    {"country": "NL", "name": "Royal Netherlands Army (KL)", "active": 21300, "reserves": 6275, "paramilitary": 5910, "tanks": 18, "ifv_apc": 612, "artillery": 27, "mlrs": 0, "key_equipment": "Leopard 2A6 (leased from Germany), CV9035NL, Boxer, PzH 2000, Fennek scout", "detail": "Tank-light; integrated with German tank battalions; PzH 2000 to Ukraine"},
    {"country": "BE", "name": "Belgian Land Component", "active": 9550, "reserves": 4750, "paramilitary": 0, "tanks": 0, "ifv_apc": 690, "artillery": 14, "mlrs": 0, "key_equipment": "Piranha IIIC, Dingo 2, Pandur, Mortier 105mm LG", "detail": "No tanks since 2014; CaMo program French Scorpion; light/wheeled focus"},
    {"country": "DK", "name": "Royal Danish Army", "active": 16500, "reserves": 11400, "paramilitary": 50500, "tanks": 51, "ifv_apc": 240, "artillery": 12, "mlrs": 0, "key_equipment": "Leopard 2A7, CV9035DK, Piranha V, M109A5, Caesar 8×8 (ordered)", "detail": "Donated artillery + Leopard 1 to Ukraine; Caesar SPH ordered; HQ deployed Latvia"},
    {"country": "NO", "name": "Norwegian Army (Hæren)", "active": 9290, "reserves": 40000, "paramilitary": 0, "tanks": 36, "ifv_apc": 240, "artillery": 24, "mlrs": 12, "key_equipment": "Leopard 2A4NO, K2 Black Panther (54 ordered), CV9030N, K9 Thunder, MLRS (ordered)", "detail": "K2 deal 2023 (€1.65B); Arctic warfare; northern Norway Russian border"},
    {"country": "FI", "name": "Finnish Army (Maavoimat)", "active": 23800, "reserves": 238000, "paramilitary": 2700, "tanks": 200, "ifv_apc": 1262, "artillery": 700, "mlrs": 109, "key_equipment": "Leopard 2A4/2A6, BMP-2MD, CV9030FIN, Pasi XA-180, K9 Moukari, M270 MLRS", "detail": "Largest reserve mobilization in Europe; long Russia border; 1300km artillery range"},
    {"country": "SE", "name": "Swedish Army (Armén)", "active": 16000, "reserves": 11200, "paramilitary": 22000, "tanks": 110, "ifv_apc": 904, "artillery": 26, "mlrs": 0, "key_equipment": "Strv 122 (Leopard 2A5), CV9040, Patgb 360 Pansarterrängbil, Archer 8×8 SPH, BvS10", "detail": "Conscript revival; Archer SPH innovative; new NATO member"},
    {"country": "GR", "name": "Hellenic Army", "active": 93000, "reserves": 220900, "paramilitary": 4000, "tanks": 1244, "ifv_apc": 2100, "artillery": 1920, "mlrs": 152, "key_equipment": "Leopard 2A6 HEL, Leopard 1A5, M48A5, BMP-1, M270 MLRS, PzH 2000, M109A5", "detail": "Largest tank fleet in EU; Aegean defense vs Turkey; conscript backbone"},
    {"country": "RO", "name": "Romanian Land Forces", "active": 35000, "reserves": 50000, "paramilitary": 79900, "tanks": 437, "ifv_apc": 1545, "artillery": 838, "mlrs": 188, "key_equipment": "TR-85M1 Bizonul, T-55, M1A2 SEPv3 (54 ordered), MLI-84, Piranha V, ATMOS 2000, HIMARS", "detail": "Black Sea NATO; HIMARS deployed; M1A2 acquisition; Russian border"},
    {"country": "HU", "name": "Hungarian Defence Forces (ground)", "active": 22700, "reserves": 20000, "paramilitary": 12000, "tanks": 44, "ifv_apc": 365, "artillery": 30, "mlrs": 0, "key_equipment": "Leopard 2A7+, T-72M1 (retiring), Lynx KF41, Gidrán 4×4, PzH 2000, BTR-80", "detail": "Lynx KF41 produced locally with Rheinmetall; Leopard 2A7 modernization"},
    {"country": "CZ", "name": "Czech Land Forces", "active": 24400, "reserves": 4800, "paramilitary": 3100, "tanks": 119, "ifv_apc": 460, "artillery": 90, "mlrs": 0, "key_equipment": "T-72M4CZ, Leopard 2A4 (15 from Germany), CV9030CZ (ordered), Pandur II, DANA SPH", "detail": "Leopard 2A4 from Germany 'ring exchange'; CV90 acquisition; ammo hub for Ukraine"},
    {"country": "SK", "name": "Slovak Land Forces", "active": 12000, "reserves": 0, "paramilitary": 0, "tanks": 22, "ifv_apc": 327, "artillery": 68, "mlrs": 26, "key_equipment": "T-72M (most donated to Ukraine), BVP-2, Patria AMV (ordered), Zuzana 2 SPH, RM-70 MLRS", "detail": "Zuzana 2 sold to Ukraine; T-72 donated; Leopard 2A4 from Germany; Patria AMV"},
    {"country": "BG", "name": "Bulgarian Land Forces", "active": 16300, "reserves": 3000, "paramilitary": 1500, "tanks": 90, "ifv_apc": 600, "artillery": 416, "mlrs": 24, "key_equipment": "T-72M1/M2, BMP-1/23, MT-LB, 2S1 Gvozdika, BM-21, Stryker (ordered)", "detail": "Stryker acquisition; Black Sea posture; Soviet-era fleet"},
    {"country": "PT", "name": "Portuguese Army", "active": 16200, "reserves": 211900, "paramilitary": 24700, "tanks": 37, "ifv_apc": 360, "artillery": 90, "mlrs": 0, "key_equipment": "Leopard 2A6, M60A3 (retired), Pandur II, M113, M114 howitzers, M109A5", "detail": "Leopard 2A6 from Netherlands; expeditionary CAR/Mali; small but capable"},
    {"country": "AT", "name": "Austrian Land Forces (Bundesheer Heer)", "active": 14800, "reserves": 144900, "paramilitary": 0, "tanks": 56, "ifv_apc": 477, "artillery": 90, "mlrs": 0, "key_equipment": "Leopard 2A4, Ulan IFV, Pandur, M109A5", "detail": "Neutral; conscript-based; militia mobilization; €16B modernization 2024-2032"},
    {"country": "CH", "name": "Swiss Army", "active": 21000, "reserves": 110000, "paramilitary": 0, "tanks": 134, "ifv_apc": 1041, "artillery": 213, "mlrs": 0, "key_equipment": "Pz 87 Leopard 2 WE (upgraded), Schützenpanzer 2000 (CV90), Piranha IIIC, M109 KAWEST", "detail": "Conscript militia; alpine defense; CV90 indigenous variant; neutrality"},
    {"country": "HR", "name": "Croatian Land Army", "active": 11250, "reserves": 18343, "paramilitary": 0, "tanks": 75, "ifv_apc": 282, "artillery": 86, "mlrs": 18, "key_equipment": "M-84A4, M-95 Degman, Patria AMV, BVP M-80, PzH 2000 (ordered 12), HIMARS (8 ordered)", "detail": "PzH 2000 + HIMARS NATO upgrade; Patria AMV; Bradley acquisition (89 from US)"},
    {"country": "SI", "name": "Slovenian Armed Forces (ground)", "active": 5500, "reserves": 1500, "paramilitary": 4400, "tanks": 75, "ifv_apc": 217, "artillery": 18, "mlrs": 0, "key_equipment": "M-84, T-55S1 (retired), Pandur, Patria AMV-XP (ordered), Centauro II, F-2000", "detail": "Boxer 8×8 program; Centauro II from Italy; small Alpine NATO force"},
    {"country": "EE", "name": "Estonian Land Forces (Maavägi)", "active": 6000, "reserves": 25000, "paramilitary": 12000, "tanks": 0, "ifv_apc": 137, "artillery": 76, "mlrs": 18, "key_equipment": "CV9035EE, K9 Kõu (24 from S.Korea), Patria Pasi, K-300P Bastion (ordered), HIMARS (6 ordered)", "detail": "K9 Korean SPH; HIMARS first Baltic operator; Kaitseliit volunteer corps"},
    {"country": "LV", "name": "Latvian Land Forces", "active": 7100, "reserves": 5500, "paramilitary": 8000, "tanks": 4, "ifv_apc": 300, "artillery": 47, "mlrs": 6, "key_equipment": "CVR(T) Scimitar/Spartan, ASCOD 2 (ordered), K9 Thunder (47 ordered), HIMARS (6 ordered)", "detail": "K9 Korean SPH; HIMARS acquisition; Zemessardze National Guard; conscription"},
    {"country": "LT", "name": "Lithuanian Land Forces", "active": 14150, "reserves": 7050, "paramilitary": 14400, "tanks": 0, "ifv_apc": 305, "artillery": 42, "mlrs": 8, "key_equipment": "Boxer Vilkas, M113A2, PzH 2000 (ex-German), Caesar 8×8 (18 ordered), HIMARS (8 ordered)", "detail": "PzH 2000 from Germany; Boxer Vilkas indigenous variant; HIMARS; NATO eFP host"},

    # ── Eurasia & ex-Soviet ──
    {"country": "UA", "name": "Ukrainian Ground Forces", "active": 850000, "reserves": 900000, "paramilitary": 102000, "tanks": 1200, "ifv_apc": 5000, "artillery": 3500, "mlrs": 360, "key_equipment": "T-72/64BV, T-80BV, T-84 Oplot, Leopard 2A6, Challenger 2, Abrams M1A1, BMP-2, CV90, M2 Bradley, PzH 2000, Caesar, HIMARS, M270, Bohdana", "detail": "Largest land war in Europe since WWII; mixed Western/Soviet inventory; mass mobilization"},
    {"country": "BY", "name": "Belarus Ground Forces", "active": 45350, "reserves": 289500, "paramilitary": 110000, "tanks": 595, "ifv_apc": 1490, "artillery": 600, "mlrs": 348, "key_equipment": "T-72BM/B3, BMP-2, BMP-3, BTR-80, 2S19 Msta-S, Polonez (Chinese A200), Iskander-M (Russian)", "detail": "Hosts Russian tactical nukes; Wagner group based; Union State Army training"},
    {"country": "AM", "name": "Armed Forces of Armenia (ground)", "active": 41850, "reserves": 210000, "paramilitary": 4300, "tanks": 109, "ifv_apc": 345, "artillery": 343, "mlrs": 80, "key_equipment": "T-72A/B, BMP-1/2, BTR-70, 2S1 Gvozdika, BM-21, Iskander-E, TOS-1A", "detail": "Heavy losses 2020 Nagorno-Karabakh war; Russian withdrawal; pivoting West"},
    {"country": "AZ", "name": "Azerbaijani Land Forces", "active": 66950, "reserves": 300000, "paramilitary": 15000, "tanks": 570, "ifv_apc": 1431, "artillery": 740, "mlrs": 230, "key_equipment": "T-90S, T-72M1 (Belarus upgrade), BMP-3, BTR-80A, 2S19 Msta-S, T-122 Sakarya, Lynx, Bayraktar TB2", "detail": "Won 2020 Nagorno-Karabakh war; drone-centric doctrine; Israeli/Turkish supplied"},
    {"country": "GE", "name": "Georgian Defense Forces (ground)", "active": 20650, "reserves": 0, "paramilitary": 11700, "tanks": 174, "ifv_apc": 137, "artillery": 350, "mlrs": 33, "key_equipment": "T-72-SIM-1, BMP-1/2, Cobra II, ZTS Dana SPH, GRADLAR, RM-70 Vampir", "detail": "Post-2008 reform; NATO partnership; light force; abandoned conscription 2016"},
    {"country": "MD", "name": "National Army of Moldova", "active": 5150, "reserves": 0, "paramilitary": 2100, "tanks": 0, "ifv_apc": 270, "artillery": 148, "mlrs": 11, "key_equipment": "BMD-1 (limited), TAB-71 (Romanian), 2S9 Nona, BM-21 Grad", "detail": "Smallest in Europe; constitutionally neutral; Transnistria frozen conflict"},
    {"country": "KZ", "name": "Kazakhstan Ground Forces", "active": 39000, "reserves": 0, "paramilitary": 31500, "tanks": 1240, "ifv_apc": 2000, "artillery": 970, "mlrs": 600, "key_equipment": "T-72BA/B, BTR-80A, BMP-2, 2S5 Hyacinth-S, BM-21, Smerch, Iskander-M (alleged)", "detail": "Largest Central Asian; Russian-trained; Belarus/Ukraine rebalance ongoing"},
    {"country": "UZ", "name": "Uzbekistan Ground Forces", "active": 38000, "reserves": 0, "paramilitary": 20000, "tanks": 420, "ifv_apc": 715, "artillery": 487, "mlrs": 108, "key_equipment": "T-72, T-64, BMP-2, BTR-80, 2S3 Akatsiya, BM-21, Tochka-U", "detail": "Largest Central Asian by personnel; Russian-supplied; Afghan border"},
    {"country": "TM", "name": "Turkmenistan Ground Forces", "active": 36500, "reserves": 0, "paramilitary": 0, "tanks": 670, "ifv_apc": 1116, "artillery": 269, "mlrs": 80, "key_equipment": "T-90S, T-72, BMP-1/2, BTR-80, 2S1, BM-21 Grad", "detail": "T-90 acquisition; isolationist neutrality; Iran/Afghan border"},
    {"country": "KG", "name": "Kyrgyz Land Forces", "active": 8500, "reserves": 0, "paramilitary": 9500, "tanks": 150, "ifv_apc": 387, "artillery": 251, "mlrs": 21, "key_equipment": "T-72, BMP-1/2, BRDM-2, 2S1 Gvozdika, BM-21, Bayraktar TB2 (recent)", "detail": "Tajik border clashes 2022; Bayraktar acquisition; CSTO member"},
    {"country": "TJ", "name": "Tajik Ground Forces", "active": 7300, "reserves": 0, "paramilitary": 7500, "tanks": 30, "ifv_apc": 33, "artillery": 23, "mlrs": 10, "key_equipment": "T-72, BMP-1, BTR-60/70, BM-21, D-30 howitzer", "detail": "Smallest CSTO; Russian 201st Base hosted; Afghan border"},

    # ── Middle East ──
    {"country": "SA", "name": "Royal Saudi Land Forces (RSLF)", "active": 75000, "reserves": 25000, "paramilitary": 130000, "tanks": 1062, "ifv_apc": 4900, "artillery": 1135, "mlrs": 270, "key_equipment": "M1A2S Abrams, AMX-30S, M2 Bradley, LAV-25, M109A5, M270 MLRS, ASTROS II, PLZ-45", "detail": "Mixed US/French/Chinese; Yemen ops; SANG separate national guard"},
    {"country": "AE", "name": "UAE Land Forces", "active": 44000, "reserves": 0, "paramilitary": 0, "tanks": 540, "ifv_apc": 1538, "artillery": 405, "mlrs": 121, "key_equipment": "Leclerc, BMP-3, Patria AMV, NIMR, G6 Rhino SPH, ASTROS II, Caracal", "detail": "Modernized 'Little Sparta'; Yemen ops; expeditionary"},
    {"country": "IL", "name": "Israel Defence Forces (IDF Ground)", "active": 126000, "reserves": 360000, "paramilitary": 8000, "tanks": 1370, "ifv_apc": 11420, "artillery": 530, "mlrs": 30, "key_equipment": "Merkava Mk IV Barak, Namer APC, Eitan 8×8, Achzarit, M109A5, MARS II MLRS, PULS", "detail": "Highly mechanized; multi-front war (Gaza/Lebanon); Trophy APS innovation"},
    {"country": "JO", "name": "Royal Jordanian Land Force", "active": 65000, "reserves": 65000, "paramilitary": 15000, "tanks": 390, "ifv_apc": 1250, "artillery": 595, "mlrs": 16, "key_equipment": "Challenger 1 (Al Hussein), M60A3 Phoenix, M109A5, Mistral, Type 90 (Chinese)", "detail": "British-influenced; Iraq/Syria border; KAFAT special ops"},
    {"country": "IQ", "name": "Iraqi Ground Forces", "active": 193000, "reserves": 0, "paramilitary": 232000, "tanks": 327, "ifv_apc": 2200, "artillery": 245, "mlrs": 33, "key_equipment": "M1A1M Abrams, T-72M1, BMP-1, BTR-94, M109A5, BM-21, Hawkei (Australian)", "detail": "Post-ISIS rebuild; PMF (Hashd) parallel; Iran-aligned militias"},
    {"country": "KW", "name": "Kuwait Army", "active": 17500, "reserves": 23700, "paramilitary": 7100, "tanks": 218, "ifv_apc": 633, "artillery": 218, "mlrs": 27, "key_equipment": "M1A2K Abrams, BMP-3, Desert Warrior, M109A5, Smerch", "detail": "Small but advanced; US ally; M1A2K Abrams; Iraqi border"},
    {"country": "QA", "name": "Qatar Land Force", "active": 12000, "reserves": 0, "paramilitary": 5000, "tanks": 90, "ifv_apc": 730, "artillery": 100, "mlrs": 4, "key_equipment": "Leopard 2A7, AMX-30 (retired), VBCI, AMX-10P, PzH 2000, ASTROS II", "detail": "Leopard 2A7 buyer; small but rich; gas field defense"},
    {"country": "OM", "name": "Royal Army of Oman", "active": 25000, "reserves": 0, "paramilitary": 4400, "tanks": 117, "ifv_apc": 351, "artillery": 233, "mlrs": 0, "key_equipment": "M60A3, Challenger 2 OM, Piranha, M109A5, FH-77B howitzer", "detail": "British-trained; Hormuz Strait; Yemeni border; Royal Guard separate"},
    {"country": "BH", "name": "Royal Bahraini Army", "active": 8500, "reserves": 0, "paramilitary": 11000, "tanks": 180, "ifv_apc": 290, "artillery": 100, "mlrs": 9, "key_equipment": "M60A3 TTS, M113, AIFV-B-C25, M109A5, ASTROS II", "detail": "Small US ally; royal protection; 5th Fleet host"},
    {"country": "LB", "name": "Lebanese Armed Forces (ground)", "active": 60000, "reserves": 0, "paramilitary": 20000, "tanks": 357, "ifv_apc": 1244, "artillery": 235, "mlrs": 25, "key_equipment": "M48A5 Patton, T-54/55, M113, Marder (donated), M198, M109A5, BM-21", "detail": "US/French aid; Hezbollah parallel structure; economic crisis"},
    {"country": "YE", "name": "Yemen Armed Forces (govt + factional)", "active": 80000, "reserves": 0, "paramilitary": 71200, "tanks": 700, "ifv_apc": 1150, "artillery": 1100, "mlrs": 295, "key_equipment": "T-55/72, M60A1, BMP-2, BTR-60, BM-21, 2S1, Houthi captured Russian/Chinese mix", "detail": "Civil war fragmented; Houthi-controlled north; Saudi-backed govt south"},
    {"country": "AF", "name": "Afghanistan Armed Forces (Taliban Islamic Emirate)", "active": 150000, "reserves": 0, "paramilitary": 0, "tanks": 175, "ifv_apc": 700, "artillery": 240, "mlrs": 0, "key_equipment": "T-55/62 (limited), M113 (US legacy), Humvee, M-30/M-20 howitzers, captured Western gear", "detail": "US-supplied legacy fleet; ISKP counter-insurgency; women excluded"},

    # ── Africa ──
    {"country": "DZ", "name": "Algerian People's National Army (ground)", "active": 130000, "reserves": 150000, "paramilitary": 187200, "tanks": 880, "ifv_apc": 2030, "artillery": 1000, "mlrs": 282, "key_equipment": "T-90SA, T-72M, BMP-2, BTR-80, 2S3 Akatsiya, BM-30 Smerch, Iskander-E", "detail": "Africa's largest; Russian-equipped; Sahel border security; Mali tensions"},
    {"country": "MA", "name": "Royal Moroccan Army (FAR ground)", "active": 175000, "reserves": 150000, "paramilitary": 50000, "tanks": 894, "ifv_apc": 1668, "artillery": 1226, "mlrs": 92, "key_equipment": "M1A2 SEPv3 Abrams (delivering), M60A3, VAB, Ratel, M109A5, ATMOS 2000", "detail": "M1A2 acquisition; Western Sahara walls; US/French/Israeli equipped"},
    {"country": "ET", "name": "Ethiopian National Defence Force (ground)", "active": 162000, "reserves": 0, "paramilitary": 75000, "tanks": 400, "ifv_apc": 600, "artillery": 700, "mlrs": 50, "key_equipment": "T-72, T-62, T-55, BMP-1, BRDM, 2S1 Gvozdika, BM-21, BM-30 (limited)", "detail": "Tigray war legacy; Eritrea/Sudan tensions; Russian-equipped"},
    {"country": "ER", "name": "Eritrean Ground Forces", "active": 200000, "reserves": 120000, "paramilitary": 0, "tanks": 270, "ifv_apc": 100, "artillery": 250, "mlrs": 75, "key_equipment": "T-54/55, T-72, BMP-1, BTR-152, D-30, BM-21", "detail": "Massive conscription; 'Africa's North Korea'; Tigray war participant"},
    {"country": "NG", "name": "Nigerian Army", "active": 130000, "reserves": 0, "paramilitary": 82000, "tanks": 286, "ifv_apc": 956, "artillery": 339, "mlrs": 21, "key_equipment": "T-72, Vickers Mk 3, Type 69-II, AML-90, VT-4 (ordered), Otokar Cobra, BMP-3", "detail": "Boko Haram counter-insurgency; ECOWAS lead; VT-4 Chinese MBT acquisition"},
    {"country": "ZA", "name": "South African National Defence Force (army)", "active": 40250, "reserves": 12000, "paramilitary": 0, "tanks": 195, "ifv_apc": 1750, "artillery": 191, "mlrs": 65, "key_equipment": "Olifant Mk 2, Ratel IFV, Mamba APC, G6 Rhino SPH, Bateleur MLRS, Badger (Hoefyster)", "detail": "Aging equipment; capability decline; G6 SPH exports"},
    {"country": "AO", "name": "Angolan Army", "active": 100000, "reserves": 0, "paramilitary": 10000, "tanks": 350, "ifv_apc": 530, "artillery": 970, "mlrs": 99, "key_equipment": "T-72M, T-62, BMP-1/2, BMP-3, BTR-80, 2S1, BM-21, BM-27 Uragan", "detail": "Largest in central Africa; Soviet legacy; Cabinda enclave; Cuban legacy"},
    {"country": "SD", "name": "Sudanese Armed Forces (SAF)", "active": 102500, "reserves": 0, "paramilitary": 70000, "tanks": 460, "ifv_apc": 410, "artillery": 778, "mlrs": 660, "key_equipment": "T-72AV, Type 96, Type 85, BMP-2, BTR-80, 2S1, BM-21, WS-2 (Chinese MLRS)", "detail": "Active civil war vs RSF; UAE/Egypt backing; air support critical"},
    {"country": "TN", "name": "Tunisian Army", "active": 27000, "reserves": 12000, "paramilitary": 12000, "tanks": 84, "ifv_apc": 530, "artillery": 217, "mlrs": 0, "key_equipment": "M60A3 Patton, AML-90, M113, M109A5, M114 howitzer", "detail": "US-trained; Libya border; ISIS counter-terror in Sahel"},
    {"country": "LY", "name": "Libyan Forces (GNU/LNA split)", "active": 35000, "reserves": 0, "paramilitary": 0, "tanks": 150, "ifv_apc": 1000, "artillery": 500, "mlrs": 60, "key_equipment": "T-72, T-55, BMP-1, BM-21 Grad, M114 howitzer, mixed Russian/Egyptian/Turkish supply", "detail": "Civil war fragmented; GNU (Tripoli) vs LNA (Tobruk); Wagner/UAE/Turkish proxies"},
    {"country": "KE", "name": "Kenya Defence Forces (army)", "active": 24100, "reserves": 0, "paramilitary": 0, "tanks": 78, "ifv_apc": 282, "artillery": 64, "mlrs": 0, "key_equipment": "Vickers Mk 3, Cadillac Gage Stingray, Panhard AML, M-46 130mm, Bofors L40", "detail": "Al-Shabaab counter-terror in Somalia; British-trained; AMISOM lead"},
    {"country": "TZ", "name": "Tanzania People's Defence Force (army)", "active": 23000, "reserves": 80000, "paramilitary": 1400, "tanks": 60, "ifv_apc": 80, "artillery": 200, "mlrs": 75, "key_equipment": "Type 59 (Chinese), Type 62, BTR-152, Type 63 SPG, BM-21, JN-45 SPH (Chinese)", "detail": "Chinese-supplied; SADC peacekeeping; Mozambique deployment"},
    {"country": "UG", "name": "Uganda People's Defence Force (army)", "active": 45000, "reserves": 10000, "paramilitary": 1800, "tanks": 239, "ifv_apc": 92, "artillery": 252, "mlrs": 12, "key_equipment": "T-90S, T-72M1, T-55, BMP-2, BTR-60, D-30, BM-21", "detail": "T-90 only sub-Saharan operator (with Uganda); ADF/DRC ops; AMISOM"},
    {"country": "RW", "name": "Rwanda Defence Force (RDF ground)", "active": 33000, "reserves": 0, "paramilitary": 2000, "tanks": 50, "ifv_apc": 200, "artillery": 95, "mlrs": 16, "key_equipment": "T-55, T-72 (limited), BMP, RG-31 Nyala, D-30, BM-21", "detail": "Mozambique counter-insurgency; DRC tensions; well-trained for size"},
    {"country": "GH", "name": "Ghana Army", "active": 11500, "reserves": 0, "paramilitary": 0, "tanks": 0, "ifv_apc": 138, "artillery": 84, "mlrs": 0, "key_equipment": "Tarantula, MOWAG Piranha, Ratel, M-46 130mm, ZPU-2 AAA", "detail": "Light force; ECOWAS lead; UN peacekeeping (Mali, CAR, Lebanon)"},
    {"country": "SN", "name": "Senegalese Army", "active": 13800, "reserves": 0, "paramilitary": 5000, "tanks": 0, "ifv_apc": 235, "artillery": 30, "mlrs": 0, "key_equipment": "AML-60/90, M3 Panhard, PT-76, M-101 howitzer, Mistral SAM", "detail": "Light expeditionary; ECOMIG Gambia; UN peacekeeping; French training"},
    {"country": "CI", "name": "Côte d'Ivoire Army", "active": 22000, "reserves": 12000, "paramilitary": 1500, "tanks": 10, "ifv_apc": 41, "artillery": 36, "mlrs": 6, "key_equipment": "T-55, AMX-13, Mamba APC, BTR-80, M-3, BM-21", "detail": "Post-civil war rebuild; French support; ECOWAS member"},
    {"country": "CM", "name": "Cameroonian Army", "active": 14200, "reserves": 0, "paramilitary": 9000, "tanks": 80, "ifv_apc": 250, "artillery": 100, "mlrs": 6, "key_equipment": "Type 63 (Chinese), T-55, V-150, Ratel, M101, RM-70 MLRS", "detail": "Boko Haram operations; Anglophone crisis; French/Chinese mix"},
    {"country": "TD", "name": "Chad National Army (ANT)", "active": 35000, "reserves": 0, "paramilitary": 9500, "tanks": 60, "ifv_apc": 230, "artillery": 5, "mlrs": 0, "key_equipment": "T-55, AML-60/90, BMP-1, ERC-90, mortars", "detail": "G5 Sahel core; counter-Boko Haram; French support; rebel pressure"},
    {"country": "NE", "name": "Nigerien Armed Forces", "active": 12000, "reserves": 0, "paramilitary": 5400, "tanks": 0, "ifv_apc": 180, "artillery": 17, "mlrs": 0, "key_equipment": "AML-60/90, BTR-3, RG-31, M-3 howitzer, mortars", "detail": "2023 coup; French withdrawal; Wagner replacement; Sahel jihadist threats"},
    {"country": "ML", "name": "Malian Armed Forces (FAMa)", "active": 13000, "reserves": 0, "paramilitary": 7800, "tanks": 33, "ifv_apc": 175, "artillery": 26, "mlrs": 4, "key_equipment": "T-55, T-72 (Russian-supplied), BTR-60/152, BMP-1, BM-21, Su-25 air support", "detail": "Coup junta; Wagner-supported; Russia pivot; jihadist insurgency"},
    {"country": "BF", "name": "Burkina Faso Armed Forces", "active": 12000, "reserves": 0, "paramilitary": 250, "tanks": 0, "ifv_apc": 90, "artillery": 18, "mlrs": 0, "key_equipment": "AML-90, EE-9, M3 Panhard, mortars, Volunteer Defenders (VDP)", "detail": "Coup junta; AES alliance with Mali/Niger; jihadist crisis; Russian pivot"},
    {"country": "MR", "name": "Mauritanian Army", "active": 16000, "reserves": 0, "paramilitary": 5000, "tanks": 35, "ifv_apc": 75, "artillery": 220, "mlrs": 0, "key_equipment": "T-55, T-54, AML-60/90, M101, D-74, mortars", "detail": "G5 Sahel; AQIM border ops; French training; small but stable"},
    {"country": "BJ", "name": "Beninese Armed Forces", "active": 7000, "reserves": 0, "paramilitary": 2500, "tanks": 0, "ifv_apc": 32, "artillery": 12, "mlrs": 0, "key_equipment": "M-8 Greyhound, BTR-60, BRDM-2, mortars", "detail": "Northern jihadist threats; small force; ECOWAS member"},
    {"country": "TG", "name": "Togolese Armed Forces", "active": 8550, "reserves": 0, "paramilitary": 0, "tanks": 0, "ifv_apc": 68, "artillery": 4, "mlrs": 0, "key_equipment": "M-8, AML-90, EE-9 Cascavel, EE-3 Jararaca, mortars", "detail": "Northern jihadist spillover; ECOMOG legacy"},
    {"country": "GA", "name": "Gabonese Armed Forces", "active": 6500, "reserves": 0, "paramilitary": 2000, "tanks": 0, "ifv_apc": 70, "artillery": 4, "mlrs": 0, "key_equipment": "AML-60/90, EE-9, VAB, mortars, Mistral SAM", "detail": "2023 coup; French training; small Gulf of Guinea force"},
    {"country": "CG", "name": "Republic of Congo Army", "active": 8000, "reserves": 0, "paramilitary": 2000, "tanks": 25, "ifv_apc": 80, "artillery": 20, "mlrs": 4, "key_equipment": "T-54/55, T-34 (legacy), BTR-152, BTR-60, D-30, BM-21", "detail": "Brazzaville; Russian/Cuban legacy"},
    {"country": "CD", "name": "Armed Forces of the DR Congo (FARDC)", "active": 134000, "reserves": 0, "paramilitary": 0, "tanks": 153, "ifv_apc": 252, "artillery": 158, "mlrs": 24, "key_equipment": "T-72, T-55, PT-76, BMP-1, BTR-60, D-30, BM-21, Wagner support", "detail": "M23/Rwanda war; Wagner instructors; vast eastern conflict zones"},
    {"country": "CF", "name": "Central African Armed Forces (FACA)", "active": 8150, "reserves": 0, "paramilitary": 1000, "tanks": 0, "ifv_apc": 39, "artillery": 0, "mlrs": 0, "key_equipment": "BRDM-2, ACMAT VLRA, technicals, Wagner support", "detail": "Wagner-stabilized; rebel groups; small French legacy force"},
    {"country": "SS", "name": "South Sudan People's Defence Force", "active": 185000, "reserves": 0, "paramilitary": 0, "tanks": 110, "ifv_apc": 130, "artillery": 60, "mlrs": 13, "key_equipment": "T-72, T-55, BMP-1, BTR-80, BM-21 Grad, mortars (Ukrainian/Belarusian-supplied)", "detail": "Civil war veterans; oil-funded; Sudan border; Israeli-trained elite"},
    {"country": "MZ", "name": "Mozambique Armed Defence Forces", "active": 11200, "reserves": 0, "paramilitary": 0, "tanks": 80, "ifv_apc": 140, "artillery": 110, "mlrs": 0, "key_equipment": "T-55, BMP-1, BTR-60/152, D-30, BM-21 (limited)", "detail": "Cabo Delgado ISIS-Mozambique; SADC/Rwanda support"},
    {"country": "ZW", "name": "Zimbabwe Defence Forces", "active": 30000, "reserves": 0, "paramilitary": 21800, "tanks": 40, "ifv_apc": 280, "artillery": 230, "mlrs": 60, "key_equipment": "Type 59 (Chinese), Type 69, BTR-152, EE-9, RM-70, BM-21", "detail": "Chinese-trained; aging Russian/Chinese mix; SADC peacekeeping"},
    {"country": "ZM", "name": "Zambia Army", "active": 13500, "reserves": 0, "paramilitary": 1400, "tanks": 75, "ifv_apc": 70, "artillery": 95, "mlrs": 50, "key_equipment": "T-54/55, Type 59, BTR-60, D-30, BM-21", "detail": "Light infantry; Chinese-supplied; landlocked SADC"},
    {"country": "BW", "name": "Botswana Defence Force", "active": 9000, "reserves": 0, "paramilitary": 0, "tanks": 26, "ifv_apc": 85, "artillery": 18, "mlrs": 0, "key_equipment": "SK-105 Kürassier, RAM-2000, V-150 Commando, M-110A2 (retired)", "detail": "Small but professional; anti-poaching; Mozambique deployment"},
    {"country": "NA", "name": "Namibian Army", "active": 9200, "reserves": 0, "paramilitary": 6000, "tanks": 12, "ifv_apc": 25, "artillery": 67, "mlrs": 5, "key_equipment": "T-34 (legacy), T-54 (limited), BTR-60, Casspir, D-30, BM-21", "detail": "Small post-independence force; SADC member; anti-poaching"},

    # ── Asia (additional) ──
    {"country": "ID", "name": "Indonesian Army (TNI-AD)", "active": 300000, "reserves": 400000, "paramilitary": 280000, "tanks": 332, "ifv_apc": 1430, "artillery": 800, "mlrs": 86, "key_equipment": "Leopard 2RI, AMX-13, Marder 1A3 (German-supplied), Pindad Anoa 6×6, Caesar, ASTROS II, RM-70 Vampir", "detail": "Largest in SE Asia; KOSTRAD strike force; Papua/Aceh ops"},
    {"country": "TH", "name": "Royal Thai Army", "active": 245000, "reserves": 200000, "paramilitary": 113700, "tanks": 805, "ifv_apc": 1350, "artillery": 2473, "mlrs": 60, "key_equipment": "VT-4 (Chinese), M60A3, T-84 Oplot-T, BTR-3E1, M109A5, ATMOS 2000, WS-1B (Chinese MLRS)", "detail": "Junta-influenced; China pivot (VT-4); Myanmar border tensions"},
    {"country": "MY", "name": "Malaysian Army (TDM)", "active": 80000, "reserves": 51600, "paramilitary": 24600, "tanks": 48, "ifv_apc": 1170, "artillery": 414, "mlrs": 36, "key_equipment": "PT-91M Pendekar, Adnan IFV, Condor, AV8 Gempita, ASTROS II, FH-2000", "detail": "Conscription suspended; Sabah ESSCOM ops; mixed inventory"},
    {"country": "PH", "name": "Philippine Army", "active": 100000, "reserves": 100000, "paramilitary": 40000, "tanks": 26, "ifv_apc": 760, "artillery": 282, "mlrs": 12, "key_equipment": "Sabrah light tank (Israeli), M113A2, V-150 Commando, M-101A1, ATMOS 2000, BrahMos (ordered)", "detail": "BrahMos first ASEAN buyer; Mindanao counter-insurgency; SCS posture"},
    {"country": "MM", "name": "Myanmar Army (Tatmadaw Kyi)", "active": 350000, "reserves": 0, "paramilitary": 107250, "tanks": 555, "ifv_apc": 1380, "artillery": 1700, "mlrs": 80, "key_equipment": "T-72S, T-55, MBT-2000 (Chinese), BTR-3, BTR-80, M-46 130mm, BM-21, SH-1 SPH", "detail": "Civil war; PDF/EAOs resistance; Russian/Chinese supply; junta rule"},
    {"country": "BD", "name": "Bangladesh Army", "active": 160000, "reserves": 0, "paramilitary": 63900, "tanks": 320, "ifv_apc": 535, "artillery": 1335, "mlrs": 70, "key_equipment": "MBT-2000 (Chinese), Type 69, Type 59, BTR-80, Otokar Cobra, WS-22 MLRS", "detail": "UN peacekeeping leader; Forces Goal 2030; Chinese-supplied"},
    {"country": "LK", "name": "Sri Lanka Army", "active": 200000, "reserves": 5500, "paramilitary": 11000, "tanks": 122, "ifv_apc": 339, "artillery": 921, "mlrs": 22, "key_equipment": "T-55AM2, Type 69-II, Type 63, BMP-1/2, RM-70, Type 81 SPH", "detail": "Post-LTTE downsizing; financial crisis; Indian/Chinese balancing"},
    {"country": "NP", "name": "Nepal Army", "active": 96800, "reserves": 0, "paramilitary": 92000, "tanks": 0, "ifv_apc": 165, "artillery": 95, "mlrs": 0, "key_equipment": "WZ-551, Casspir, M114, M101 howitzers, mortars", "detail": "UN peacekeeping major contributor; Maoist legacy; non-aligned"},
    {"country": "KH", "name": "Royal Cambodian Army", "active": 75000, "reserves": 0, "paramilitary": 67000, "tanks": 200, "ifv_apc": 224, "artillery": 428, "mlrs": 30, "key_equipment": "T-54/55, Type 59, BMP-1, BTR-60, D-30, BM-21, Type 90 (Chinese)", "detail": "Chinese-supplied; Ream Naval Base controversy; border with Thailand"},
    {"country": "LA", "name": "Lao People's Armed Forces", "active": 29100, "reserves": 0, "paramilitary": 100000, "tanks": 130, "ifv_apc": 70, "artillery": 100, "mlrs": 9, "key_equipment": "T-72B (Russian-supplied recently), T-54/55, PT-76, BMP-1, M-46, BM-21", "detail": "T-72B acquisition; Vietnam-aligned; Mekong river ops"},
    {"country": "BT", "name": "Royal Bhutan Army", "active": 8000, "reserves": 0, "paramilitary": 1000, "tanks": 0, "ifv_apc": 0, "artillery": 12, "mlrs": 0, "key_equipment": "Mortars, light arms, rifles; trained by India", "detail": "India-trained; landlocked; small Doklam border"},
    {"country": "BN", "name": "Royal Brunei Land Force", "active": 4900, "reserves": 700, "paramilitary": 2250, "tanks": 0, "ifv_apc": 90, "artillery": 24, "mlrs": 0, "key_equipment": "VAB, Black Hawk for transport, mortars, light arms", "detail": "Tiny but well-funded oil state; British/Singapore ties"},
    {"country": "TL", "name": "Timor-Leste Defence Force", "active": 2200, "reserves": 0, "paramilitary": 0, "tanks": 0, "ifv_apc": 0, "artillery": 0, "mlrs": 0, "key_equipment": "Light arms, patrol boats, F-FDTL", "detail": "Post-independence; Portuguese/Australian support; tiny coastal force"},
    {"country": "MV", "name": "Maldives National Defence Force", "active": 4000, "reserves": 0, "paramilitary": 0, "tanks": 0, "ifv_apc": 6, "artillery": 0, "mlrs": 0, "key_equipment": "Light arms, patrol boats, helicopters; Indian-trained", "detail": "Coast guard role primary; Indian Ocean security; pro-China shift"},
    {"country": "MN", "name": "Mongolian Armed Forces", "active": 9700, "reserves": 137000, "paramilitary": 7500, "tanks": 480, "ifv_apc": 320, "artillery": 760, "mlrs": 130, "key_equipment": "T-72A, T-55, BMP-1, BTR-60, D-30, BM-21 Grad", "detail": "Steppe Eagle exercise; Russian-supplied; small but extensive equipment"},

    # ── Latin America ──
    {"country": "MX", "name": "Mexican Army (SEDENA Ground)", "active": 217000, "reserves": 81000, "paramilitary": 0, "tanks": 0, "ifv_apc": 1295, "artillery": 165, "mlrs": 0, "key_equipment": "DN-XI Caballo, Sandcat, M-37, M101 howitzer, Otomelara 105mm", "detail": "Cartel war; National Guard separate (paramilitary); no MBTs"},
    {"country": "AR", "name": "Argentine Army (Ejército Argentino)", "active": 41000, "reserves": 0, "paramilitary": 31250, "tanks": 269, "ifv_apc": 587, "artillery": 285, "mlrs": 4, "key_equipment": "TAM 2C, M113A2, AMX-13, Patagón, M101 105mm, CITER 155mm", "detail": "Indigenous TAM medium tank; Falklands legacy; rebuilding capability"},
    {"country": "CL", "name": "Chilean Army (Ejército de Chile)", "active": 45478, "reserves": 0, "paramilitary": 60500, "tanks": 322, "ifv_apc": 1230, "artillery": 350, "mlrs": 12, "key_equipment": "Leopard 2A4 CHL, Marder 1A3, Piranha IIIC, M109A5, LAR-160 (Israeli MLRS)", "detail": "Most modernized in S.America; Leopard 2A4; mountain warfare"},
    {"country": "PE", "name": "Peruvian Army (EP)", "active": 75000, "reserves": 188000, "paramilitary": 77000, "tanks": 320, "ifv_apc": 470, "artillery": 470, "mlrs": 24, "key_equipment": "T-55, T-54, AMX-13, BMP-1, BTR-60, M101, M-46, BM-21 Grad", "detail": "Sendero Luminoso legacy; Russian/Chinese mix; modernization stalled"},
    {"country": "CO", "name": "Colombian National Army", "active": 235000, "reserves": 35000, "paramilitary": 158000, "tanks": 0, "ifv_apc": 1010, "artillery": 130, "mlrs": 0, "key_equipment": "Cascavel, M113A2, Hummer, M101, M-30 howitzer", "detail": "FARC/ELN/cartels counter-insurgency; US-trained; no MBTs (Andes terrain)"},
    {"country": "VE", "name": "Bolivarian Army of Venezuela", "active": 63000, "reserves": 220000, "paramilitary": 220000, "tanks": 178, "ifv_apc": 480, "artillery": 376, "mlrs": 25, "key_equipment": "T-72B1V, AMX-30V, BMP-3M, BTR-80A, 2S19 Msta, Smerch, BM-21", "detail": "Russian rebuild; Maduro regime; sanctions impact; Guyana Esequibo claims"},
    {"country": "EC", "name": "Ecuadorian Army", "active": 24750, "reserves": 0, "paramilitary": 100, "tanks": 75, "ifv_apc": 184, "artillery": 290, "mlrs": 6, "key_equipment": "AMX-13, EE-9 Cascavel, M-114, M-198 howitzer, mortars", "detail": "Internal armed conflict 2024 (cartels); state of emergency ops"},
    {"country": "BO", "name": "Bolivian Army", "active": 26000, "reserves": 0, "paramilitary": 37100, "tanks": 0, "ifv_apc": 167, "artillery": 132, "mlrs": 0, "key_equipment": "EE-9 Cascavel, V-150, M101, mortars", "detail": "Conscription; coca eradication; Chinese-supplied"},
    {"country": "UY", "name": "Uruguayan Army (Ejército Nacional)", "active": 14500, "reserves": 0, "paramilitary": 800, "tanks": 35, "ifv_apc": 175, "artillery": 75, "mlrs": 0, "key_equipment": "Tiran-5 (Israeli), M24 Chaffee, M113, Otokar Cobra, M101", "detail": "UN peacekeeping (DRC, Haiti); small professional"},
    {"country": "PY", "name": "Paraguayan Army", "active": 7600, "reserves": 0, "paramilitary": 14800, "tanks": 12, "ifv_apc": 30, "artillery": 109, "mlrs": 0, "key_equipment": "M-4 Sherman (museum), EE-9 Cascavel, M-101 howitzer", "detail": "Tiny force; Chaco region; Brazil/Argentina balanced"},
    {"country": "CU", "name": "Cuban Revolutionary Armed Forces (army)", "active": 38000, "reserves": 39000, "paramilitary": 26500, "tanks": 900, "ifv_apc": 700, "artillery": 1700, "mlrs": 175, "key_equipment": "T-72M, T-62, T-55, BMP-1, BTR-60, D-30, BM-21", "detail": "Aging Soviet legacy; sanctions impact maintenance; large reserves"},
    {"country": "DO", "name": "Dominican Army (Ejército)", "active": 28000, "reserves": 0, "paramilitary": 15000, "tanks": 0, "ifv_apc": 36, "artillery": 28, "mlrs": 0, "key_equipment": "Cadillac Gage Commando, M3 Stuart (museum), M101 howitzer", "detail": "Haitian border crisis; UN peacekeeping; small US-aligned"},
    {"country": "GT", "name": "Guatemalan Army", "active": 17000, "reserves": 0, "paramilitary": 25000, "tanks": 0, "ifv_apc": 65, "artillery": 76, "mlrs": 0, "key_equipment": "M8 Greyhound (museum), V-150, M101 howitzer", "detail": "Counter-narcotics; Mexico border; small force"},
    {"country": "HN", "name": "Honduran Army", "active": 8000, "reserves": 60000, "paramilitary": 8000, "tanks": 12, "ifv_apc": 16, "artillery": 45, "mlrs": 0, "key_equipment": "Scorpion light tank, RBY Mk1, M101 howitzer", "detail": "Counter-narcotics; gang violence; US-aligned"},
    {"country": "NI", "name": "Nicaraguan Army", "active": 12000, "reserves": 0, "paramilitary": 0, "tanks": 127, "ifv_apc": 166, "artillery": 800, "mlrs": 33, "key_equipment": "T-72B1, T-55, BTR-60, BMP-1, D-30, BM-21", "detail": "Russian-supplied; Ortega regime; recent T-72B1 acquisition"},
    {"country": "SV", "name": "Salvadoran Army", "active": 20500, "reserves": 0, "paramilitary": 17000, "tanks": 0, "ifv_apc": 70, "artillery": 102, "mlrs": 0, "key_equipment": "AML-90, M37, M114 howitzer, mortars", "detail": "Bukele anti-gang state of exception; small force"},
    {"country": "GY", "name": "Guyana Defence Force", "active": 3400, "reserves": 670, "paramilitary": 0, "tanks": 0, "ifv_apc": 9, "artillery": 6, "mlrs": 0, "key_equipment": "Shorland S-55, M-46 130mm (limited)", "detail": "Tiny force; Esequibo dispute with Venezuela; oil-driven expansion"},
    {"country": "SR", "name": "Suriname National Army", "active": 1840, "reserves": 0, "paramilitary": 0, "tanks": 0, "ifv_apc": 12, "artillery": 0, "mlrs": 0, "key_equipment": "EE-9 Cascavel, EE-11 Urutu, mortars", "detail": "Tiny; Dutch legacy; Atlantic coast"},
    {"country": "TT", "name": "Trinidad and Tobago Defence Force", "active": 4063, "reserves": 0, "paramilitary": 0, "tanks": 0, "ifv_apc": 0, "artillery": 0, "mlrs": 0, "key_equipment": "Light arms, patrol boats, helicopters", "detail": "Coast guard primary role; oil state security"},

    # ── Oceania ──
    {"country": "AU", "name": "Australian Army", "active": 30700, "reserves": 19425, "paramilitary": 0, "tanks": 59, "ifv_apc": 1100, "artillery": 60, "mlrs": 12, "key_equipment": "M1A1 SA Abrams (M1A2 SEPv3 ordered), Boxer CRV, AS21 Redback (ordered), M777A2, HIMARS (ordered)", "detail": "AUKUS pillar; Project Land 400 modernization; HIMARS acquisition"},
    {"country": "NZ", "name": "New Zealand Army", "active": 4500, "reserves": 1900, "paramilitary": 0, "tanks": 0, "ifv_apc": 105, "artillery": 24, "mlrs": 0, "key_equipment": "NZLAV (LAV III), Bushmaster PMV, L118 105mm Light Gun, mortars", "detail": "No tanks; light expeditionary; Pacific peacekeeping"},
    {"country": "FJ", "name": "Republic of Fiji Military Forces", "active": 3500, "reserves": 6000, "paramilitary": 0, "tanks": 0, "ifv_apc": 7, "artillery": 0, "mlrs": 0, "key_equipment": "Light arms, mortars, BTR-50 (limited), Chinese supplied", "detail": "UN peacekeeping (Sinai, Iraq, Lebanon); China/Australia balancing"},
    {"country": "PG", "name": "Papua New Guinea Defence Force", "active": 3600, "reserves": 0, "paramilitary": 0, "tanks": 0, "ifv_apc": 8, "artillery": 0, "mlrs": 0, "key_equipment": "Light arms, patrol boats, Bushmaster (Australian-supplied)", "detail": "Tiny force; Australian assistance; tribal security focus"},

    # ── Other smaller forces (selected) ──
    {"country": "AL", "name": "Albanian Land Forces", "active": 6500, "reserves": 0, "paramilitary": 500, "tanks": 0, "ifv_apc": 30, "artillery": 30, "mlrs": 0, "key_equipment": "T-55 (retired), BTR-60, M-30 howitzer, mortars", "detail": "NATO member; light force; modernization underway"},
    {"country": "BA", "name": "Armed Forces of Bosnia and Herzegovina", "active": 9200, "reserves": 5000, "paramilitary": 0, "tanks": 70, "ifv_apc": 130, "artillery": 230, "mlrs": 22, "key_equipment": "M-84, T-55, M113, BVP M-80, M-109A5, M-46, M-65, BM-21", "detail": "Post-Dayton; multi-ethnic; NATO partnership; large legacy stocks"},
    {"country": "MK", "name": "Army of the Republic of North Macedonia", "active": 8000, "reserves": 4850, "paramilitary": 0, "tanks": 31, "ifv_apc": 184, "artillery": 144, "mlrs": 0, "key_equipment": "T-72A (Ukraine donation), BTR-70, M113, M-30, MT-LB, JLTV (US supplied)", "detail": "Newest NATO member; Stryker acquisition; donated T-72 to Ukraine"},
    {"country": "ME", "name": "Armed Forces of Montenegro", "active": 2350, "reserves": 0, "paramilitary": 0, "tanks": 0, "ifv_apc": 6, "artillery": 12, "mlrs": 0, "key_equipment": "M93 howitzer, MORS mortars, Oshkosh M-ATV", "detail": "NATO member; tiny force; coastal defense"},
    {"country": "RS", "name": "Serbian Army (Vojska Srbije ground)", "active": 28150, "reserves": 50150, "paramilitary": 0, "tanks": 232, "ifv_apc": 590, "artillery": 410, "mlrs": 130, "key_equipment": "M-84A4, T-72, BVP M-80, Lazar 3, NORA-B52, OGANJ M-77, FK-3 (HQ-22)", "detail": "Largest in Balkans; Russian/Chinese SAMs; non-NATO neutral"},
    {"country": "XK", "name": "Kosovo Security Force", "active": 3000, "reserves": 800, "paramilitary": 0, "tanks": 0, "ifv_apc": 51, "artillery": 12, "mlrs": 0, "key_equipment": "Bayraktar TB2 (ordered), JLTV, mortars, M119 105mm", "detail": "Transitioning to army; KFOR-mentored; Serbia tensions; TB2 drone acquisition"},
    {"country": "CY", "name": "Cyprus National Guard", "active": 12000, "reserves": 50000, "paramilitary": 750, "tanks": 134, "ifv_apc": 396, "artillery": 234, "mlrs": 12, "key_equipment": "T-80U, AMX-30B2, BMP-3, VAB, M114, M109A5, BM-21", "detail": "Conscription; T-80U largest in Europe outside Russia; Buffer Zone"},
    {"country": "MT", "name": "Armed Forces of Malta", "active": 1950, "reserves": 0, "paramilitary": 0, "tanks": 0, "ifv_apc": 0, "artillery": 0, "mlrs": 0, "key_equipment": "Light arms, patrol boats, Skyranger AAA", "detail": "Tiny force; coast guard primary; EU member"},
    {"country": "IS", "name": "Iceland Coast Guard (no army)", "active": 250, "reserves": 0, "paramilitary": 0, "tanks": 0, "ifv_apc": 0, "artillery": 0, "mlrs": 0, "key_equipment": "Patrol vessels, Bombardier Dash 8 Q300, AS332 Super Puma", "detail": "No standing army; NATO airbase Keflavík; coast guard only"},
    {"country": "IE", "name": "Irish Defence Forces (army)", "active": 7300, "reserves": 1700, "paramilitary": 0, "tanks": 0, "ifv_apc": 80, "artillery": 12, "mlrs": 0, "key_equipment": "Mowag Piranha III, RG-32M Galten, L118 105mm Light Gun", "detail": "Neutral; UN peacekeeping; light infantry; no air defense"},
    {"country": "LU", "name": "Luxembourg Army", "active": 900, "reserves": 0, "paramilitary": 612, "tanks": 0, "ifv_apc": 48, "artillery": 0, "mlrs": 0, "key_equipment": "Dingo 2, Hummer, light arms", "detail": "Tiny NATO; reconnaissance/light infantry; Eurocorps"},
]


# ═══════════════ WORLD AIR FORCES ═══════════════
# Key data for major air forces worldwide (inventory estimates from open sources)
WORLD_AIR_FORCES = [
    # ── Top 10 by fleet size ──
    {"country": "US", "name": "United States Air Force + Navy/Marines", "total_aircraft": 13300, "fighters": 1957, "bombers": 140, "attack": 871, "transport": 945, "helicopters": 5758, "tankers": 625, "special_mission": 742, "awacs": 31, "key_types": "F-35A/B/C, F-22, F-15E/EX, F-16, B-2, B-1B, B-52H, C-17, C-130J, KC-46A, E-3 Sentry", "detail": "World's largest air force; global power projection; 5th-gen dominance"},
    {"country": "RU", "name": "Russian Aerospace Forces (VKS)", "total_aircraft": 4173, "fighters": 772, "bombers": 130, "attack": 739, "transport": 445, "helicopters": 1543, "tankers": 19, "special_mission": 132, "awacs": 11, "key_types": "Su-35S, Su-30SM, Su-57, Su-34, MiG-31BM, Tu-160M, Tu-95MS, Tu-22M3, Il-76, A-50U", "detail": "Second-largest; heavy losses in Ukraine; air defense focus"},
    {"country": "CN", "name": "People's Liberation Army Air Force (PLAAF)", "total_aircraft": 3304, "fighters": 1200, "bombers": 176, "attack": 371, "transport": 286, "helicopters": 912, "tankers": 16, "special_mission": 119, "awacs": 17, "key_types": "J-20, J-16, J-10C, J-11B, H-6K/N, Y-20, KJ-500, KJ-2000, Z-20", "detail": "Rapid modernization; 5th-gen J-20; increasing stealth bomber program (H-20)"},
    {"country": "IN", "name": "Indian Air Force (IAF)", "total_aircraft": 2296, "fighters": 564, "bombers": 0, "attack": 130, "transport": 250, "helicopters": 722, "tankers": 6, "special_mission": 48, "awacs": 3, "key_types": "Rafale, Su-30MKI, Tejas Mk1, MiG-29, Mirage 2000, C-17, C-130J, Il-76, AEW&C", "detail": "4th-largest; Rafale procurement; indigenous Tejas expanding; AMCA 5th-gen planned"},
    {"country": "EG", "name": "Egyptian Air Force", "total_aircraft": 1062, "fighters": 337, "bombers": 0, "attack": 88, "transport": 55, "helicopters": 297, "tankers": 0, "special_mission": 12, "awacs": 2, "key_types": "F-16C/D, Rafale, MiG-29M/M2, Su-35, Ka-52, AH-64D, E-2C", "detail": "Largest in Middle East/Africa; mixed US/Russian/French fleet"},
    {"country": "KR", "name": "Republic of Korea Air Force (ROKAF)", "total_aircraft": 898, "fighters": 406, "bombers": 0, "attack": 0, "transport": 35, "helicopters": 280, "tankers": 4, "special_mission": 20, "awacs": 4, "key_types": "KF-21, F-35A, F-15K, KF-16, FA-50, E-737, KC-330", "detail": "Advanced fleet; indigenous KF-21 Boramae entering service; 40 F-35As"},
    {"country": "PK", "name": "Pakistan Air Force (PAF)", "total_aircraft": 970, "fighters": 387, "bombers": 0, "attack": 90, "transport": 48, "helicopters": 328, "tankers": 4, "special_mission": 12, "awacs": 7, "key_types": "JF-17 Thunder, F-16A/B/C/D, Mirage III/V, J-10CE, ZDK-03 AEW&C, Erieye", "detail": "Sino-Pak JF-17 backbone; J-10C acquisition; nuclear-capable"},
    {"country": "JP", "name": "Japan Air Self-Defense Force (JASDF)", "total_aircraft": 743, "fighters": 297, "bombers": 0, "attack": 0, "transport": 49, "helicopters": 154, "tankers": 7, "special_mission": 35, "awacs": 17, "key_types": "F-35A/B, F-15J/DJ, F-2, E-767, E-2D, KC-767, C-2", "detail": "High-tech fleet; 147 F-35A/B planned; GCAP 6th-gen program with UK/Italy"},
    {"country": "TR", "name": "Turkish Air Force (TurAF)", "total_aircraft": 712, "fighters": 244, "bombers": 0, "attack": 80, "transport": 86, "helicopters": 210, "tankers": 7, "special_mission": 22, "awacs": 4, "key_types": "F-16C/D Block 50+, T-129 ATAK, E-737, KAAN (5th-gen prototype), Bayraktar TB2/Akinci", "detail": "NATO's 2nd-largest; indigenous KAAN 5th-gen developing; drone superpower"},
    {"country": "FR", "name": "French Air & Space Force + Aeronavale", "total_aircraft": 691, "fighters": 226, "bombers": 0, "attack": 0, "transport": 85, "helicopters": 219, "tankers": 15, "special_mission": 41, "awacs": 4, "key_types": "Rafale B/C/M, Mirage 2000-5/D, A330 MRTT, A400M, E-3F, NH90, Tigre", "detail": "Nuclear-capable (ASMPA); power projection; Operation Sentinelle; SCAF 6th-gen"},
    {"country": "GB", "name": "Royal Air Force (RAF) + Fleet Air Arm", "total_aircraft": 607, "fighters": 137, "bombers": 0, "attack": 0, "transport": 57, "helicopters": 228, "tankers": 14, "special_mission": 30, "awacs": 6, "key_types": "F-35B, Typhoon FGR4, A330 Voyager, C-17, A400M, E-7 Wedgetail, Apache AH-64E, Merlin", "detail": "Carrier aviation F-35B; GCAP 6th-gen; nuclear deterrent Vanguard SLBM"},
    {"country": "DE", "name": "German Air Force (Luftwaffe)", "total_aircraft": 465, "fighters": 136, "bombers": 0, "attack": 0, "transport": 67, "helicopters": 200, "tankers": 4, "special_mission": 20, "awacs": 0, "key_types": "Typhoon, Tornado IDS/ECR, A400M, A330 MRTT, NH90, Tiger, CH-47F", "detail": "Zeitenwende modernization; €100B special fund; F-35A order (35); nuclear sharing"},
    {"country": "IT", "name": "Italian Air Force (Aeronautica Militare)", "total_aircraft": 437, "fighters": 89, "bombers": 0, "attack": 51, "transport": 45, "helicopters": 180, "tankers": 4, "special_mission": 18, "awacs": 0, "key_types": "F-35A/B, Typhoon, Tornado IDS, AMX, C-130J, KC-767, NH90, AW101", "detail": "F-35A/B fleet growing; GCAP 6th-gen partner; carrier aviation"},
    {"country": "IL", "name": "Israeli Air Force (IAF/Heyl HaAvir)", "total_aircraft": 581, "fighters": 241, "bombers": 0, "attack": 0, "transport": 34, "helicopters": 178, "tankers": 8, "special_mission": 36, "awacs": 5, "key_types": "F-35I Adir, F-15I Ra'am, F-16I Sufa, AH-64D Saraf, Heron/Hermes UAVs, G550 CAEW", "detail": "Qualitative military edge; F-35I with indigenous systems; extensive combat experience"},
    {"country": "SA", "name": "Royal Saudi Air Force (RSAF)", "total_aircraft": 848, "fighters": 281, "bombers": 0, "attack": 0, "transport": 55, "helicopters": 212, "tankers": 6, "special_mission": 18, "awacs": 5, "key_types": "F-15SA/S, Typhoon, Tornado IDS, AH-64E, E-3A, A330 MRTT", "detail": "Major F-15SA fleet; Yemen operations; Vision 2030 indigenous defense goals"},
    {"country": "AU", "name": "Royal Australian Air Force (RAAF)", "total_aircraft": 391, "fighters": 71, "bombers": 0, "attack": 0, "transport": 38, "helicopters": 113, "tankers": 7, "special_mission": 28, "awacs": 6, "key_types": "F-35A, F/A-18F Super Hornet, EA-18G Growler, E-7A Wedgetail, KC-30A, C-17, C-130J, MQ-4C, P-8A", "detail": "High-tech small force; 72 F-35As; electronic warfare; AUKUS pillar"},
    {"country": "BR", "name": "Brazilian Air Force (FAB)", "total_aircraft": 676, "fighters": 43, "bombers": 0, "attack": 99, "transport": 124, "helicopters": 260, "tankers": 2, "special_mission": 14, "awacs": 5, "key_types": "Gripen E/F, AMX, A-29 Super Tucano, KC-390, C-130, E-99, AH-2 Sabre", "detail": "Gripen E replacing F-5; KC-390 indigenous transport; Amazon surveillance"},
    {"country": "PL", "name": "Polish Air Force", "total_aircraft": 347, "fighters": 88, "bombers": 0, "attack": 0, "transport": 32, "helicopters": 143, "tankers": 0, "special_mission": 12, "awacs": 0, "key_types": "F-35A (48 ordered), F-16C/D, MiG-29, FA-50, AW101, Black Hawk, AH-64E (planned)", "detail": "NATO eastern flank; massive modernization; $35B defense budget; F-35 + K2 tank"},
    {"country": "TW", "name": "Republic of China Air Force (ROCAF)", "total_aircraft": 741, "fighters": 286, "bombers": 0, "attack": 0, "transport": 30, "helicopters": 271, "tankers": 0, "special_mission": 27, "awacs": 6, "key_types": "F-16V Viper, Mirage 2000-5, AIDC F-CK-1, E-2K Hawkeye, AH-64E, CH-47SD", "detail": "Cross-strait deterrence; 66 new F-16V Block 70; indigenous missiles; reserve mobilization"},
    {"country": "UA", "name": "Ukrainian Air Force (PSU)", "total_aircraft": 318, "fighters": 67, "bombers": 0, "attack": 19, "transport": 32, "helicopters": 120, "tankers": 0, "special_mission": 8, "awacs": 0, "key_types": "F-16AM/BM (donated), MiG-29, Su-27, Su-25, Mi-24, Mi-8, TB2 Bayraktar", "detail": "F-16 transition from MiG/Su fleet; Western donations critical; heavy attrition/reconstitution"},
    {"country": "SE", "name": "Swedish Air Force (Flygvapnet)", "total_aircraft": 210, "fighters": 96, "bombers": 0, "attack": 0, "transport": 16, "helicopters": 60, "tankers": 0, "special_mission": 8, "awacs": 2, "key_types": "Gripen C/D/E, S 100B Argus, C-130H, NH90, Black Hawk, GlobalEye (ordered)", "detail": "Indigenous Gripen; new NATO member; Baltic defense; GlobalEye AEW&C coming"},
    {"country": "GR", "name": "Hellenic Air Force (HAF)", "total_aircraft": 543, "fighters": 235, "bombers": 0, "attack": 0, "transport": 24, "helicopters": 170, "tankers": 0, "special_mission": 22, "awacs": 4, "key_types": "F-16C/D Viper, Rafale, Mirage 2000, AH-64D, E-2C, EMB-145H", "detail": "Aegean deterrence vs Turkey; Rafale + F-16V upgrade; large fleet for country size"},
    {"country": "NO", "name": "Royal Norwegian Air Force (RNoAF)", "total_aircraft": 155, "fighters": 52, "bombers": 0, "attack": 0, "transport": 7, "helicopters": 45, "tankers": 0, "special_mission": 15, "awacs": 0, "key_types": "F-35A, P-8A Poseidon, C-130J, NH90, AW101, MQ-9B", "detail": "52 F-35As; Arctic/North Atlantic focus; Russian border monitoring; P-8A maritime patrol"},
    {"country": "IR", "name": "Islamic Republic of Iran Air Force + IRGC-AF", "total_aircraft": 541, "fighters": 186, "bombers": 0, "attack": 57, "transport": 103, "helicopters": 126, "tankers": 3, "special_mission": 12, "awacs": 0, "key_types": "F-14A Tomcat, MiG-29, Su-35 (ordered), F-4 Phantom, F-5E, Shahed/Mohajer UAVs, Kowsar", "detail": "Aging fleet; F-14A unique operator; massive UAV/drone program; Su-35 acquisition from Russia"},
    {"country": "AE", "name": "UAE Air Force & Air Defence", "total_aircraft": 531, "fighters": 139, "bombers": 0, "attack": 0, "transport": 24, "helicopters": 212, "tankers": 3, "special_mission": 16, "awacs": 2, "key_types": "F-16E/F Block 60, Mirage 2000-9, Rafale (ordered), AH-64E, UH-60M, GlobalEye", "detail": "Modern Gulf force; Rafale deal; Wing Loong/Predator UAVs; Yemen ops experience"},
    {"country": "CA", "name": "Royal Canadian Air Force (RCAF)", "total_aircraft": 391, "fighters": 76, "bombers": 0, "attack": 0, "transport": 38, "helicopters": 155, "tankers": 5, "special_mission": 24, "awacs": 0, "key_types": "CF-18 Hornet, F-35A (88 ordered), CC-177 Globemaster, CC-130J, CP-140 Aurora, CH-148 Cyclone", "detail": "F-35A replacing CF-18; NORAD partner; Arctic sovereignty; CP-140 maritime patrol"},
    {"country": "ES", "name": "Spanish Air Force (EdA)", "total_aircraft": 343, "fighters": 84, "bombers": 0, "attack": 0, "transport": 42, "helicopters": 126, "tankers": 3, "special_mission": 18, "awacs": 0, "key_types": "Typhoon, F/A-18 Hornet, A400M, C-130, KC-130, AV-8B+ (Navy), NH90, Tiger", "detail": "FCAS/SCAF 6th-gen partner; Canary Islands defense; Rota base support"},
    {"country": "SG", "name": "Republic of Singapore Air Force (RSAF)", "total_aircraft": 222, "fighters": 98, "bombers": 0, "attack": 0, "transport": 22, "helicopters": 68, "tankers": 4, "special_mission": 15, "awacs": 4, "key_types": "F-35B (ordered), F-15SG, F-16C/D, G550 AEW, KC-135, AH-64D, CH-47SD", "detail": "Most advanced SE Asian air force; F-35B replacing F-16; Guam training detachment"},
    {"country": "ID", "name": "Indonesian Air Force (TNI-AU)", "total_aircraft": 447, "fighters": 33, "bombers": 0, "attack": 32, "transport": 96, "helicopters": 195, "tankers": 0, "special_mission": 15, "awacs": 0, "key_types": "Su-27/30, F-16C/D, Rafale (ordered), T-50i, CN-235, C-130, Super Tucano", "detail": "Archipelago coverage; Rafale deal replacing aging fleet; maritime patrol focus"},

    # ── Additional Europe ──
    {"country": "NL", "name": "Royal Netherlands Air Force (KLu)", "total_aircraft": 195, "fighters": 46, "bombers": 0, "attack": 0, "transport": 14, "helicopters": 79, "tankers": 0, "special_mission": 8, "awacs": 0, "key_types": "F-35A (52 ordered), F-16AM/BM, AH-64E, CH-47F, NH90, A330 MRTT (MMU pool)", "detail": "F-35A replacing F-16; nuclear sharing role; A330 MRTT pooled with Lux/DE/NO/CZ/BE"},
    {"country": "BE", "name": "Belgian Air Component", "total_aircraft": 138, "fighters": 49, "bombers": 0, "attack": 0, "transport": 11, "helicopters": 60, "tankers": 0, "special_mission": 4, "awacs": 0, "key_types": "F-16AM/BM, F-35A (34 ordered), A400M, NH90, A109", "detail": "Aging F-16s; F-35A from 2025; nuclear sharing role"},
    {"country": "FI", "name": "Finnish Air Force (Ilmavoimat)", "total_aircraft": 158, "fighters": 55, "bombers": 0, "attack": 0, "transport": 4, "helicopters": 14, "tankers": 0, "special_mission": 8, "awacs": 0, "key_types": "F/A-18C/D Hornet, F-35A (64 ordered), Hawk Mk 51/66, NH90, C-295M", "detail": "New NATO member; F-35A replacing Hornet; long Russia border; air-defense focused"},
    {"country": "DK", "name": "Royal Danish Air Force (Flyvevåbnet)", "total_aircraft": 99, "fighters": 30, "bombers": 0, "attack": 0, "transport": 8, "helicopters": 30, "tankers": 0, "special_mission": 8, "awacs": 0, "key_types": "F-16A/B (retiring), F-35A (27 received), MH-60R, EH-101 Merlin, C-130J", "detail": "F-35A operational; Greenland/Arctic sovereignty; donated F-16s to Ukraine"},
    {"country": "CH", "name": "Swiss Air Force", "total_aircraft": 162, "fighters": 56, "bombers": 0, "attack": 0, "transport": 0, "helicopters": 86, "tankers": 0, "special_mission": 5, "awacs": 0, "key_types": "F/A-18C/D Hornet, F-5E/F Tiger II, F-35A (36 ordered), Super Puma, EC635", "detail": "Neutral; F-35A controversial purchase; air policing focus; alpine terrain ops"},
    {"country": "AT", "name": "Austrian Air Force", "total_aircraft": 153, "fighters": 15, "bombers": 0, "attack": 0, "transport": 8, "helicopters": 80, "tankers": 0, "special_mission": 4, "awacs": 0, "key_types": "Eurofighter Typhoon Tranche 1, Saab 105 (retiring), C-130K, OH-58, S-70 Black Hawk", "detail": "Neutral; small Typhoon fleet; alpine SAR; €560M modernization plan"},
    {"country": "RO", "name": "Romanian Air Force", "total_aircraft": 134, "fighters": 35, "bombers": 0, "attack": 0, "transport": 16, "helicopters": 70, "tankers": 0, "special_mission": 4, "awacs": 0, "key_types": "F-16AM/BM, MiG-21 (retired), F-35A (32 ordered), C-27J, C-130, IAR-330 Puma", "detail": "Black Sea NATO frontline; F-16 expansion ex-Norway; F-35A from 2030s"},
    {"country": "CZ", "name": "Czech Air Force", "total_aircraft": 100, "fighters": 14, "bombers": 0, "attack": 0, "transport": 14, "helicopters": 49, "tankers": 0, "special_mission": 4, "awacs": 0, "key_types": "JAS-39 Gripen C/D (leased), L-159 ALCA, F-35A (24 ordered), C-295, Mi-171, H-1Z (ordered)", "detail": "Gripen lease ending; F-35A from 2031; AH-1Z attack helo procurement"},
    {"country": "HU", "name": "Hungarian Air Force", "total_aircraft": 80, "fighters": 14, "bombers": 0, "attack": 0, "transport": 8, "helicopters": 40, "tankers": 0, "special_mission": 4, "awacs": 0, "key_types": "JAS-39 Gripen C/D, A319, A350 (ordered), H145M, H225M, KC-390 (ordered)", "detail": "Small Gripen fleet; Embraer KC-390 multi-role tankers; modernization push"},
    {"country": "SK", "name": "Slovak Air Force", "total_aircraft": 35, "fighters": 0, "bombers": 0, "attack": 0, "transport": 8, "helicopters": 23, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "F-16C/D Block 70 (delivering), Mi-17, UH-60M, L-39 (retired), C-27J Spartan", "detail": "MiG-29 transferred to Ukraine; F-16 Block 70 from 2024; air policing gap"},
    {"country": "BG", "name": "Bulgarian Air Force", "total_aircraft": 70, "fighters": 14, "bombers": 0, "attack": 6, "transport": 6, "helicopters": 38, "tankers": 0, "special_mission": 4, "awacs": 0, "key_types": "MiG-29, Su-25, F-16 Block 70 (ordered 16), C-27J, AS532 Cougar, Mi-17", "detail": "MiG-29 aging; F-16 Block 70 from 2025; Black Sea air policing"},
    {"country": "HR", "name": "Croatian Air Force", "total_aircraft": 75, "fighters": 12, "bombers": 0, "attack": 0, "transport": 4, "helicopters": 50, "tankers": 0, "special_mission": 4, "awacs": 0, "key_types": "Rafale F3-R (12 ex-French), MiG-21 (retired), Mi-171Sh, OH-58D, Pilatus PC-9", "detail": "Rafale operational; F-16 deal collapsed; small but modernizing"},
    {"country": "PT", "name": "Portuguese Air Force", "total_aircraft": 110, "fighters": 28, "bombers": 0, "attack": 0, "transport": 16, "helicopters": 50, "tankers": 1, "special_mission": 12, "awacs": 0, "key_types": "F-16AM/BM Block 15 MLU, P-3C Orion, C-130H, C-295M, EH-101 Merlin, KC-390 (ordered)", "detail": "Atlantic/maritime patrol focus; F-16 MLU mid-life upgrade; KC-390 Embraer"},
    {"country": "RS", "name": "Serbian Air Force & Air Defence", "total_aircraft": 88, "fighters": 18, "bombers": 0, "attack": 27, "transport": 5, "helicopters": 35, "tankers": 0, "special_mission": 3, "awacs": 0, "key_types": "MiG-29 (Russian transfer), J-22 Orao, G-4 Super Galeb, Mi-17, Rafale (12 ordered)", "detail": "Rafale deal 2024 (€2.7B); shifting from Russian to French"},
    {"country": "FI", "name": "Finnish Border Guard Air Wing", "total_aircraft": 25, "fighters": 0, "bombers": 0, "attack": 0, "transport": 0, "helicopters": 20, "tankers": 0, "special_mission": 5, "awacs": 0, "key_types": "AB/B412, AS332L1, Dornier 228", "detail": "Maritime/border surveillance; SAR; not main air force"},
    {"country": "BY", "name": "Belarus Air Force & Air Defence", "total_aircraft": 173, "fighters": 35, "bombers": 0, "attack": 24, "transport": 14, "helicopters": 92, "tankers": 0, "special_mission": 4, "awacs": 0, "key_types": "MiG-29, Su-30SM (Russian), Su-25K, Yak-130, Mi-24, Mi-8, Su-24M (returned)", "detail": "Russian-supplied; nuclear capable Su-30SM; tactical nukes hosted"},
    {"country": "LT", "name": "Lithuanian Air Force", "total_aircraft": 18, "fighters": 0, "bombers": 0, "attack": 0, "transport": 5, "helicopters": 13, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "L-39ZA Albatros, Mi-8 (retired), C-27J Spartan, Black Hawk", "detail": "No fighters; relies on NATO Baltic Air Policing; UH-60M expansion"},
    {"country": "LV", "name": "Latvian Air Force", "total_aircraft": 12, "fighters": 0, "bombers": 0, "attack": 0, "transport": 0, "helicopters": 12, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Mi-17 (retired), UH-60M Black Hawk, AN/TPS-77 radar", "detail": "Tiny force; NATO air policing dependency; UH-60M Black Hawks"},
    {"country": "EE", "name": "Estonian Air Force", "total_aircraft": 10, "fighters": 0, "bombers": 0, "attack": 0, "transport": 4, "helicopters": 4, "tankers": 0, "special_mission": 2, "awacs": 0, "key_types": "An-2, R-44 Robinson, IAI Heron 1 (ordered)", "detail": "Smallest Baltic force; NATO Baltic Air Policing host (Ämari)"},
    {"country": "BG", "name": "Bulgarian Naval Aviation", "total_aircraft": 12, "fighters": 0, "bombers": 0, "attack": 0, "transport": 0, "helicopters": 12, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "AS565MB Panther, Mi-14 Haze (retired)", "detail": "Black Sea ASW; small force"},

    # ── Africa (major) ──
    {"country": "DZ", "name": "Algerian Air Force", "total_aircraft": 540, "fighters": 89, "bombers": 0, "attack": 39, "transport": 78, "helicopters": 230, "tankers": 6, "special_mission": 8, "awacs": 0, "key_types": "Su-30MKA, MiG-29S/UB, Su-24MK, Yak-130, Il-78MP, Mi-28NE, Mi-26", "detail": "Africa's 2nd-largest; predominantly Russian; Sahel security"},
    {"country": "ZA", "name": "South African Air Force (SAAF)", "total_aircraft": 217, "fighters": 26, "bombers": 0, "attack": 13, "transport": 21, "helicopters": 110, "tankers": 0, "special_mission": 8, "awacs": 0, "key_types": "JAS-39 Gripen C/D, BAE Hawk Mk 120, Rooivalk attack heli, A109LUH, Oryx, C-130BZ", "detail": "Most advanced sub-Saharan; Gripen serviceability issues; rated decline"},
    {"country": "MA", "name": "Royal Moroccan Air Force", "total_aircraft": 251, "fighters": 73, "bombers": 0, "attack": 24, "transport": 64, "helicopters": 79, "tankers": 4, "special_mission": 7, "awacs": 0, "key_types": "F-16C/D Block 52+, Mirage F1, F-5E, AH-64E Apache, KC-130, C-130", "detail": "F-16V upgrade; Apache from 2024; Western Sahara ops; key US ally"},
    {"country": "NG", "name": "Nigerian Air Force", "total_aircraft": 184, "fighters": 23, "bombers": 0, "attack": 14, "transport": 25, "helicopters": 95, "tankers": 0, "special_mission": 27, "awacs": 0, "key_types": "JF-17 Thunder, F-7Ni, Alpha Jet, A-29 Super Tucano, Mi-35, AW109", "detail": "Counter-insurgency Boko Haram; JF-17 from Pakistan; A-29 from US"},
    {"country": "ET", "name": "Ethiopian Air Force", "total_aircraft": 90, "fighters": 22, "bombers": 0, "attack": 7, "transport": 14, "helicopters": 40, "tankers": 0, "special_mission": 7, "awacs": 0, "key_types": "Su-27, MiG-23, Mi-35, Y-12, An-12, Mi-8/17, Bayraktar TB2", "detail": "Tigray war veteran; Eritrea border tensions; TB2 drones critical"},
    {"country": "AO", "name": "Angolan Air Force", "total_aircraft": 295, "fighters": 78, "bombers": 0, "attack": 13, "transport": 30, "helicopters": 85, "tankers": 0, "special_mission": 8, "awacs": 0, "key_types": "Su-30K, MiG-23, Su-24MK, Mi-24/35, Alouette III, C-295, Embraer EMB-314", "detail": "Largest in central Africa; Russian-derived; UNITA legacy procurement"},
    {"country": "SD", "name": "Sudanese Air Force", "total_aircraft": 198, "fighters": 50, "bombers": 0, "attack": 25, "transport": 18, "helicopters": 65, "tankers": 0, "special_mission": 10, "awacs": 0, "key_types": "MiG-29, Su-25, Su-24M, A-5 Fantan, Mi-24, Mi-17", "detail": "Active civil war (SAF vs RSF); equipment losses; Saudi/Egyptian support"},
    {"country": "LY", "name": "Libyan Air Force (split)", "total_aircraft": 117, "fighters": 5, "bombers": 0, "attack": 11, "transport": 11, "helicopters": 30, "tankers": 0, "special_mission": 5, "awacs": 0, "key_types": "MiG-23, Su-22 (limited operational), Mi-25, Mirage F1 (LNA), Wing Loong II UAVs", "detail": "Split between GNU (Tripoli) and LNA (Tobruk); UAE/Russian/Turkish drones"},
    {"country": "TN", "name": "Tunisian Air Force", "total_aircraft": 154, "fighters": 12, "bombers": 0, "attack": 16, "transport": 5, "helicopters": 105, "tankers": 0, "special_mission": 4, "awacs": 0, "key_types": "F-5E/F Tiger II, T-6C Texan II, OH-58D, UH-1N, AB-205, Westland Scout", "detail": "Small force; US training partner; T-6C light attack adoption"},
    {"country": "KE", "name": "Kenya Air Force", "total_aircraft": 110, "fighters": 24, "bombers": 0, "attack": 0, "transport": 11, "helicopters": 60, "tankers": 0, "special_mission": 7, "awacs": 0, "key_types": "F-5E/F, Hawk Mk52, Tucano, MD500, Z-9, AT-802L Longsword", "detail": "Al-Shabaab counter-terrorism in Somalia; aging F-5 fleet"},
    {"country": "TZ", "name": "Tanzania People's Defence Air Force", "total_aircraft": 35, "fighters": 11, "bombers": 0, "attack": 0, "transport": 6, "helicopters": 14, "tankers": 0, "special_mission": 4, "awacs": 0, "key_types": "J-7G, F-7TZ (Chinese), K-8E, Y-12, Bell 206, Robinson R44", "detail": "Small force; Chinese-supplied; trains in Zimbabwe"},
    {"country": "UG", "name": "Uganda People's Defence Air Force", "total_aircraft": 65, "fighters": 11, "bombers": 0, "attack": 0, "transport": 5, "helicopters": 35, "tankers": 0, "special_mission": 4, "awacs": 0, "key_types": "Su-30MK2, MiG-21 (retired), Bell 412, Mi-24, AT-802", "detail": "Su-30 only sub-Saharan operator; AMISOM Somalia ops; ADF in DRC"},
    {"country": "GH", "name": "Ghana Air Force", "total_aircraft": 32, "fighters": 0, "bombers": 0, "attack": 0, "transport": 12, "helicopters": 14, "tankers": 0, "special_mission": 6, "awacs": 0, "key_types": "K-8, MB-339, Bell 412, Z-9, AS350 Ecureuil, A330 (presidential)", "detail": "Trainers and transport only; ECOMOG legacy; UN peacekeeping support"},
    {"country": "SN", "name": "Senegalese Air Force", "total_aircraft": 24, "fighters": 0, "bombers": 0, "attack": 0, "transport": 8, "helicopters": 14, "tankers": 0, "special_mission": 2, "awacs": 0, "key_types": "EMB-314 Super Tucano, R-235 Guerrier, Mi-35P, AS355", "detail": "Light strike Tucanos; ECOWAS rapid reaction"},
    {"country": "CI", "name": "Côte d'Ivoire Air Force", "total_aircraft": 12, "fighters": 0, "bombers": 0, "attack": 0, "transport": 4, "helicopters": 6, "tankers": 0, "special_mission": 2, "awacs": 0, "key_types": "Mi-24 (limited), Mi-8/17, Cessna 421, Embraer ERJ-135", "detail": "Small force; rebuilt after civil war; French support"},
    {"country": "CM", "name": "Cameroonian Air Force", "total_aircraft": 50, "fighters": 7, "bombers": 0, "attack": 0, "transport": 12, "helicopters": 28, "tankers": 0, "special_mission": 3, "awacs": 0, "key_types": "Alpha Jet, MB-326, Z-9, Mi-17, A-29 Super Tucano (ordered)", "detail": "Boko Haram operations; A-29 acquisition; aging Alpha Jets"},
    {"country": "RW", "name": "Rwanda Air Force", "total_aircraft": 22, "fighters": 0, "bombers": 0, "attack": 0, "transport": 6, "helicopters": 14, "tankers": 0, "special_mission": 2, "awacs": 0, "key_types": "Mi-24V, Mi-17, BN-2 Defender, Cessna 208, AS350, R-44", "detail": "Mozambique counter-insurgency expedition; small but active"},

    # ── Middle East ──
    {"country": "JO", "name": "Royal Jordanian Air Force (RJAF)", "total_aircraft": 247, "fighters": 53, "bombers": 0, "attack": 0, "transport": 18, "helicopters": 161, "tankers": 0, "special_mission": 9, "awacs": 0, "key_types": "F-16AM/BM, F-16V Block 70 (8 ordered), AH-1F Cobra, UH-60M, AT-802", "detail": "Strong US ally; F-16V deliveries; Black Hawk fleet; Iraq/Syria border"},
    {"country": "KW", "name": "Kuwait Air Force", "total_aircraft": 109, "fighters": 39, "bombers": 0, "attack": 0, "transport": 12, "helicopters": 50, "tankers": 0, "special_mission": 4, "awacs": 0, "key_types": "F/A-18C/D Hornet, Eurofighter Typhoon (28 delivered), AH-64D, KC-130J, Caracal", "detail": "Hornet + Typhoon mixed force; F-35 program declined"},
    {"country": "QA", "name": "Qatar Emiri Air Force (QEAF)", "total_aircraft": 162, "fighters": 67, "bombers": 0, "attack": 0, "transport": 24, "helicopters": 49, "tankers": 4, "special_mission": 12, "awacs": 0, "key_types": "Rafale, F-15QA, Eurofighter Typhoon, AH-64E, NH90, A330 MRTT", "detail": "Triple-source modernization; F-15QA most advanced Eagle; CENTCOM hub"},
    {"country": "OM", "name": "Royal Air Force of Oman", "total_aircraft": 116, "fighters": 23, "bombers": 0, "attack": 0, "transport": 14, "helicopters": 65, "tankers": 0, "special_mission": 8, "awacs": 0, "key_types": "F-16C/D Block 50, Eurofighter Typhoon, Hawk Mk 203, NH90, Super Lynx, C-130", "detail": "Hormuz Strait surveillance; Typhoon + F-16 mix; UK partnership"},
    {"country": "BH", "name": "Royal Bahraini Air Force (RBAF)", "total_aircraft": 50, "fighters": 22, "bombers": 0, "attack": 0, "transport": 6, "helicopters": 18, "tankers": 0, "special_mission": 4, "awacs": 0, "key_types": "F-16C/D Block 40, F-16V Block 70 (16 ordered), AH-1E Cobra, F-5E, AB-212", "detail": "5th Fleet HQ; F-16V upgrade; small but central US ally"},
    {"country": "IQ", "name": "Iraqi Air Force", "total_aircraft": 175, "fighters": 35, "bombers": 0, "attack": 18, "transport": 25, "helicopters": 88, "tankers": 0, "special_mission": 9, "awacs": 0, "key_types": "F-16IQ, T-50IQ, KAI T-50, EMB-314 Super Tucano, Mi-17/35, AC-208", "detail": "Rebuilding post-2014; ISIS legacy ops; F-16IQ readiness issues; KF-21 interest"},
    {"country": "SY", "name": "Syrian Arab Air Force", "total_aircraft": 326, "fighters": 53, "bombers": 0, "attack": 30, "transport": 14, "helicopters": 165, "tankers": 0, "special_mission": 4, "awacs": 0, "key_types": "MiG-29, MiG-23, Su-22, MiG-21, Mi-25, Mi-17", "detail": "Heavy attrition civil war; Russian air support legacy; degraded readiness post-Assad"},
    {"country": "LB", "name": "Lebanese Air Force", "total_aircraft": 65, "fighters": 0, "bombers": 0, "attack": 6, "transport": 6, "helicopters": 50, "tankers": 0, "special_mission": 3, "awacs": 0, "key_types": "A-29 Super Tucano, AC-208, UH-1H Huey II, R-44, AW139, Cessna 208", "detail": "Light strike only; counter-Hezbollah/IS borders; US support"},
    {"country": "YE", "name": "Yemeni Air Force (Hadi gov)", "total_aircraft": 30, "fighters": 8, "bombers": 0, "attack": 6, "transport": 4, "helicopters": 12, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "MiG-29SMT, F-5E, Su-22 (legacy), Mi-17/24", "detail": "Civil war fragmented; Saudi-aligned; Houthi captures"},
    {"country": "AF", "name": "Afghanistan Air Force (Taliban)", "total_aircraft": 60, "fighters": 0, "bombers": 0, "attack": 5, "transport": 8, "helicopters": 47, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Mi-17, Mi-35 (limited), MD-530F, A-29 Super Tucano, C-208 (degraded)", "detail": "US-supplied legacy fleet; limited operational; ISKP counter-insurgency"},

    # ── Asia (additional) ──
    {"country": "VN", "name": "Vietnam People's Air Force", "total_aircraft": 271, "fighters": 75, "bombers": 0, "attack": 35, "transport": 35, "helicopters": 110, "tankers": 0, "special_mission": 16, "awacs": 0, "key_types": "Su-30MK2V, Su-22M4, MiG-21 (retired), Mi-8/17, Yak-130, T-6C", "detail": "South China Sea posture; T-6C from US (post-2017 thaw); aging Russian fleet"},
    {"country": "MY", "name": "Royal Malaysian Air Force (TUDM)", "total_aircraft": 185, "fighters": 43, "bombers": 0, "attack": 13, "transport": 22, "helicopters": 71, "tankers": 4, "special_mission": 12, "awacs": 0, "key_types": "Su-30MKM, F/A-18D Hornet, BAE Hawk 100/200, FA-50 (18 ordered), CN-235", "detail": "FA-50 Block 20 from S.Korea; F/A-18 + Su-30 mix; LCA program"},
    {"country": "PH", "name": "Philippine Air Force (PAF)", "total_aircraft": 198, "fighters": 12, "bombers": 0, "attack": 19, "transport": 28, "helicopters": 110, "tankers": 0, "special_mission": 12, "awacs": 0, "key_types": "FA-50PH, A-29 Super Tucano, S-211 (retired), C-130J, AW109, Black Hawk", "detail": "South China Sea tensions; FA-50 upgrade plan; US-Philippine training"},
    {"country": "MM", "name": "Myanmar Air Force (Tatmadaw Lay)", "total_aircraft": 282, "fighters": 60, "bombers": 0, "attack": 35, "transport": 25, "helicopters": 115, "tankers": 0, "special_mission": 22, "awacs": 0, "key_types": "MiG-29B/SE, JF-17M, Su-30SME (ordered), K-8W, F-7M, Mi-35, Yak-130", "detail": "Civil war ground attack; Russian/Chinese supply; sanctions impact"},
    {"country": "BD", "name": "Bangladesh Air Force", "total_aircraft": 166, "fighters": 44, "bombers": 0, "attack": 10, "transport": 17, "helicopters": 65, "tankers": 0, "special_mission": 8, "awacs": 0, "key_types": "F-7BG/BGI, MiG-29UB/SE, Yak-130, K-8W, L-39ZA, Mi-17, Bell 212", "detail": "F-7 fleet aging; Forces Goal 2030 modernization; Chinese-supplied"},
    {"country": "LK", "name": "Sri Lanka Air Force", "total_aircraft": 102, "fighters": 8, "bombers": 0, "attack": 12, "transport": 11, "helicopters": 56, "tankers": 0, "special_mission": 5, "awacs": 0, "key_types": "F-7BS Skybolt, Kfir C2/C7, MiG-27 (retired), Mi-17, Bell 212/412, K-8", "detail": "Post-LTTE; aging fleet; financial constraints; Indian Ocean patrols"},
    {"country": "KH", "name": "Royal Cambodian Air Force", "total_aircraft": 30, "fighters": 0, "bombers": 0, "attack": 0, "transport": 6, "helicopters": 22, "tankers": 0, "special_mission": 2, "awacs": 0, "key_types": "Z-9, Mi-17, Y-12, AS350, BN-2 Islander", "detail": "Tiny force; Chinese-supplied; Ream Naval Base controversy"},
    {"country": "KZ", "name": "Kazakhstan Air Defence Forces", "total_aircraft": 235, "fighters": 96, "bombers": 0, "attack": 14, "transport": 14, "helicopters": 90, "tankers": 0, "special_mission": 11, "awacs": 0, "key_types": "Su-30SM, Su-27, MiG-31, MiG-29, Su-25, Mi-35M, EC145", "detail": "Largest Central Asian; Russian + EU dual procurement; Su-30SM new"},
    {"country": "UZ", "name": "Uzbekistan Air & Air Defence Forces", "total_aircraft": 175, "fighters": 30, "bombers": 0, "attack": 27, "transport": 12, "helicopters": 95, "tankers": 0, "special_mission": 8, "awacs": 0, "key_types": "Su-27, MiG-29, Su-25, Su-24, Mi-24, Mi-8/17", "detail": "Soviet legacy; balanced Russia/US; modernization slow"},
    {"country": "TM", "name": "Turkmenistan Air Force", "total_aircraft": 90, "fighters": 24, "bombers": 0, "attack": 35, "transport": 5, "helicopters": 22, "tankers": 0, "special_mission": 4, "awacs": 0, "key_types": "MiG-29, Su-25 (largest operator), Mi-24, Mi-8, Bayraktar TB2", "detail": "Su-25 frogfoot largest fleet; Iranian/Afghan border; TB2 drones"},
    {"country": "MN", "name": "Mongolian Air Force", "total_aircraft": 27, "fighters": 8, "bombers": 0, "attack": 0, "transport": 11, "helicopters": 8, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "MiG-21 (limited), An-26, Y-12, Mi-8, Mi-24 (retired)", "detail": "Tiny force; Russian/Chinese mix; Steppe Eagle exercise"},
    {"country": "NZ", "name": "Royal New Zealand Air Force (RNZAF)", "total_aircraft": 47, "fighters": 0, "bombers": 0, "attack": 0, "transport": 12, "helicopters": 26, "tankers": 0, "special_mission": 9, "awacs": 0, "key_types": "P-8A Poseidon, C-130J-30, NH90, A109, T-6C Texan II, KC-130 (retired)", "detail": "No combat jets since 2001; P-8A maritime patrol; Pacific security"},

    # ── Latin America ──
    {"country": "MX", "name": "Mexican Air Force (FAM)", "total_aircraft": 354, "fighters": 6, "bombers": 0, "attack": 47, "transport": 44, "helicopters": 235, "tankers": 0, "special_mission": 22, "awacs": 0, "key_types": "F-5E (limited), PC-7, T-6C, EMB-145MP, Black Hawk, MD-530F, EC725", "detail": "Counter-narcotics focus; aging F-5; PC-7 trainers; Sedena operations"},
    {"country": "AR", "name": "Argentine Air Force (FAA)", "total_aircraft": 158, "fighters": 24, "bombers": 0, "attack": 16, "transport": 28, "helicopters": 60, "tankers": 1, "special_mission": 17, "awacs": 0, "key_types": "F-16AM/BM (24 from Denmark), A-4AR Fightinghawk (retired), IA-63 Pampa, KC-130, Bell 412", "detail": "F-16AM acquisition 2024; Falklands legacy; rebuilding capability"},
    {"country": "CL", "name": "Chilean Air Force (FACh)", "total_aircraft": 270, "fighters": 46, "bombers": 0, "attack": 0, "transport": 24, "helicopters": 70, "tankers": 4, "special_mission": 10, "awacs": 0, "key_types": "F-16C/D Block 50, F-16AM/BM (Dutch), F-5E Tigre III, A-29 Super Tucano, KC-135E, C-130", "detail": "Most advanced South American; F-16 fleet; Antarctic ops"},
    {"country": "PE", "name": "Peruvian Air Force (FAP)", "total_aircraft": 232, "fighters": 27, "bombers": 0, "attack": 25, "transport": 20, "helicopters": 100, "tankers": 4, "special_mission": 15, "awacs": 0, "key_types": "MiG-29SE/UB, Mirage 2000P, Su-25, A-37B Dragonfly, KC-130, Mi-17", "detail": "Mixed Russian/French; Mirage upgrade; modernization stalled"},
    {"country": "VE", "name": "Venezuelan Bolivarian Military Aviation", "total_aircraft": 229, "fighters": 21, "bombers": 0, "attack": 23, "transport": 28, "helicopters": 109, "tankers": 0, "special_mission": 10, "awacs": 0, "key_types": "Su-30MK2, F-16A/B (limited), F-5E, K-8W, Mi-17, Mi-35M2", "detail": "Russian-supplied; F-16 grounded; sanctions impact maintenance"},
    {"country": "EC", "name": "Ecuadorian Air Force (FAE)", "total_aircraft": 88, "fighters": 21, "bombers": 0, "attack": 9, "transport": 9, "helicopters": 30, "tankers": 0, "special_mission": 8, "awacs": 0, "key_types": "Atlas Cheetah C, Kfir CE/TE, EMB-314 Super Tucano, A-29B, Bell 412, Dhruv", "detail": "Galápagos ops; Cheetah retired; Tucano backbone"},
    {"country": "CO", "name": "Colombian Aerospace Force (FAC)", "total_aircraft": 270, "fighters": 22, "bombers": 0, "attack": 60, "transport": 36, "helicopters": 130, "tankers": 0, "special_mission": 18, "awacs": 0, "key_types": "Kfir C10/C12, A-29B Super Tucano, AC-47T Spooky, Black Hawk, Mi-17, Cougar", "detail": "Counter-narcotics + FARC legacy; Tucano deep strike; F-16 acquisition planned"},
    {"country": "BO", "name": "Bolivian Air Force", "total_aircraft": 58, "fighters": 0, "bombers": 0, "attack": 6, "transport": 18, "helicopters": 24, "tankers": 0, "special_mission": 6, "awacs": 0, "key_types": "K-8VB Karakorum, T-33 (retired), C-130, Bell UH-1H, Eurocopter AS350", "detail": "Light force; counter-narcotics; coca eradication"},
    {"country": "UY", "name": "Uruguayan Air Force", "total_aircraft": 33, "fighters": 0, "bombers": 0, "attack": 4, "transport": 8, "helicopters": 12, "tankers": 0, "special_mission": 4, "awacs": 0, "key_types": "A-37B Dragonfly, IA-58 Pucará, C-95 Bandeirante, AS532, Bell 212", "detail": "Tiny force; A-37 retiring; modernization needed"},
    {"country": "DO", "name": "Dominican Air Force (FARD)", "total_aircraft": 39, "fighters": 0, "bombers": 0, "attack": 8, "transport": 6, "helicopters": 18, "tankers": 0, "special_mission": 4, "awacs": 0, "key_types": "EMB-314 Super Tucano, OH-58 Kiowa, Bell 407, T-35 Pillán, C-208 Caravan", "detail": "Counter-narcotics; A-29 strike; Haitian border"},
    {"country": "GT", "name": "Guatemalan Air Force", "total_aircraft": 24, "fighters": 0, "bombers": 0, "attack": 2, "transport": 8, "helicopters": 11, "tankers": 0, "special_mission": 3, "awacs": 0, "key_types": "PC-7 Turbo Trainer, Bell 412/206, T-65 Buckeye, A-37 (retired)", "detail": "Tiny; counter-narcotics support"},

    # ── Oceania ──
    {"country": "FJ", "name": "Republic of Fiji Military Air Wing", "total_aircraft": 6, "fighters": 0, "bombers": 0, "attack": 0, "transport": 4, "helicopters": 2, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Y-12 (Chinese), AS355 Squirrel", "detail": "Smallest in Pacific; UN peacekeeping support"},
    {"country": "PG", "name": "PNG Defence Force Air Element", "total_aircraft": 8, "fighters": 0, "bombers": 0, "attack": 0, "transport": 6, "helicopters": 2, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "PAC P-750 XSTOL, IAI Arava (retired), Bell UH-1", "detail": "Maritime/transport; Australian assistance"},

    # ── Asia (additional) ──
    {"country": "TH", "name": "Royal Thai Air Force (RTAF)", "total_aircraft": 277, "fighters": 70, "bombers": 0, "attack": 30, "transport": 39, "helicopters": 87, "tankers": 0, "special_mission": 16, "awacs": 2, "key_types": "JAS-39 C/D Gripen, F-16A/B ADF, F-5E/F TH, Alpha Jet, Saab 340 Erieye, T-50TH, AU-23A", "detail": "Most modern in SE Asia after Singapore; Gripen + Erieye AWACS; F-35 considered then deferred"},
    {"country": "KP", "name": "Korean People's Army Air Force (KPAAF)", "total_aircraft": 940, "fighters": 458, "bombers": 80, "attack": 114, "transport": 100, "helicopters": 202, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "MiG-29, MiG-23, MiG-21/F-7, Su-25, Il-28, H-5, An-24, Mi-8/17, Hughes 500", "detail": "Massive but obsolete; fuel/parts shortages limit sortie rate; tunnel-based airfields; air defense focus"},
    {"country": "NP", "name": "Nepalese Army Air Service", "total_aircraft": 18, "fighters": 0, "bombers": 0, "attack": 0, "transport": 4, "helicopters": 14, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Mi-17, HAL Dhruv, Bell 206, Eurocopter AS350, Y-12", "detail": "No combat aircraft; HADR/UN peacekeeping focus; Himalayan rescue ops"},
    {"country": "BN", "name": "Royal Brunei Air Force", "total_aircraft": 18, "fighters": 0, "bombers": 0, "attack": 0, "transport": 4, "helicopters": 12, "tankers": 0, "special_mission": 2, "awacs": 0, "key_types": "PC-7 Mk II, S-70i Black Hawk, Bell 212/214, CN-235M, Bo 105", "detail": "Tiny coastal force; British training; oil revenue funded"},
    {"country": "LA", "name": "Lao People's Liberation Army Air Force", "total_aircraft": 31, "fighters": 0, "bombers": 0, "attack": 4, "transport": 10, "helicopters": 17, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "MiG-21 (retired), Yak-130 (4), An-26, Mi-17, Ka-32, Z-9", "detail": "Tiny mostly Russian-supplied; Yak-130 light attack from Russia 2017"},
    {"country": "TJ", "name": "Air and Air Defence Forces of Tajikistan", "total_aircraft": 18, "fighters": 0, "bombers": 0, "attack": 4, "transport": 4, "helicopters": 10, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Su-25 (4), Mi-8/17, Mi-24 (limited), L-39", "detail": "Tiny; CSTO; Russian 201st Base provides cover; Afghan border focus"},
    {"country": "KG", "name": "Kyrgyz Air Force", "total_aircraft": 14, "fighters": 0, "bombers": 0, "attack": 4, "transport": 2, "helicopters": 8, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "MiG-21 (storage), L-39 Albatros, Mi-8/17, An-26", "detail": "Practically defunct; CSTO; Russian Kant Air Base; Bayraktar TB2 acquisition 2022"},

    # ── Caucasus ──
    {"country": "AZ", "name": "Azerbaijan Air Forces", "total_aircraft": 147, "fighters": 19, "bombers": 0, "attack": 19, "transport": 26, "helicopters": 70, "tankers": 0, "special_mission": 13, "awacs": 0, "key_types": "MiG-29, Su-25, Mi-35M, Mi-17V, Bayraktar TB2, Harop loitering munition, JF-17 (planned)", "detail": "Drone-heavy; decisive in 2020 Karabakh war; TB2 + Harop + Israeli LORA; JF-17 deal with Pakistan 2024"},
    {"country": "AM", "name": "Armenian Air Force", "total_aircraft": 64, "fighters": 5, "bombers": 0, "attack": 14, "transport": 6, "helicopters": 39, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Su-30SM (4 from Russia), Su-25, Mi-24, Mi-8/17, Yak-130", "detail": "Lost 2020 war; Su-30 little used; pivoting to French/Indian arms post-Russia rift"},
    {"country": "GE", "name": "Georgian Air Force (Defence Forces aviation)", "total_aircraft": 36, "fighters": 0, "bombers": 0, "attack": 12, "transport": 6, "helicopters": 18, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Su-25KM Scorpion (Israeli upgrade), L-39, Mi-24, Mi-8, Iroquois", "detail": "Lost much in 2008 war; Su-25KM is Israeli-upgraded; NATO-aspirant training"},

    # ── Africa (Horn / East) ──
    {"country": "ER", "name": "Eritrean Air Force", "total_aircraft": 18, "fighters": 8, "bombers": 0, "attack": 4, "transport": 2, "helicopters": 4, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "MiG-29, Su-27 (limited), MB-339, Mi-17, Y-12", "detail": "Small Soviet-vintage; isolated regime; sanctions impact"},
    {"country": "DJ", "name": "Djibouti Air Force", "total_aircraft": 8, "fighters": 0, "bombers": 0, "attack": 0, "transport": 3, "helicopters": 5, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Cessna 208, AS355, Mi-17, Y-12", "detail": "Tiny; hosts US/French/Chinese/Japanese bases; key Red Sea chokepoint"},
    {"country": "SO", "name": "Somali Air Force (rebuilding)", "total_aircraft": 4, "fighters": 0, "bombers": 0, "attack": 0, "transport": 2, "helicopters": 2, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Cessna 208, Mi-17 (limited)", "detail": "Effectively non-existent; reconstituting under Turkish/UAE/Egyptian assistance"},
    {"country": "SS", "name": "South Sudan People's Defence Forces Air Wing", "total_aircraft": 16, "fighters": 0, "bombers": 0, "attack": 0, "transport": 4, "helicopters": 12, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Mi-17, Mi-24 (some), Cessna 208", "detail": "Limited to helicopter ops; civil war legacy"},

    # ── Africa (West / Sahel) ──
    {"country": "ML", "name": "Mali Air Force", "total_aircraft": 24, "fighters": 0, "bombers": 0, "attack": 6, "transport": 6, "helicopters": 12, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "L-39, Su-25 (Russian), Mi-24P, Mi-8/17, Bayraktar TB2 (Turkish)", "detail": "Junta-led; Wagner/Africa Corps support; TB2 + Su-25 from Russia 2022-23"},
    {"country": "BF", "name": "Burkina Faso Air Force", "total_aircraft": 18, "fighters": 0, "bombers": 0, "attack": 5, "transport": 4, "helicopters": 9, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "A-29B Super Tucano (3), Mi-17, Mi-35, Bell 412, AT-802L Longsword, Bayraktar Akinci", "detail": "Junta; Russian/Turkish pivot; TB2 + Akinci drone use against jihadists"},
    {"country": "NE", "name": "Niger Air Force", "total_aircraft": 14, "fighters": 0, "bombers": 0, "attack": 2, "transport": 4, "helicopters": 8, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "AS350, Mi-17, C-130H, Cessna 208, Diamond DA42", "detail": "Junta 2023; expelled French/US bases; pivoting Russian"},
    {"country": "TD", "name": "Chadian Air Force", "total_aircraft": 12, "fighters": 0, "bombers": 0, "attack": 4, "transport": 3, "helicopters": 5, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Su-25 (limited), Mi-24, PC-7, AS355, C-27J", "detail": "French support; Sahel intervention; small but battle-experienced"},
    {"country": "MR", "name": "Islamic Air Force of Mauritania", "total_aircraft": 16, "fighters": 0, "bombers": 0, "attack": 4, "transport": 4, "helicopters": 8, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "EMB-314 Super Tucano, BT-67 Basler, Y-12, Cessna 208", "detail": "Light counter-terror force; Sahel coalition"},
    {"country": "BJ", "name": "Beninese Air Force", "total_aircraft": 6, "fighters": 0, "bombers": 0, "attack": 0, "transport": 2, "helicopters": 4, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Do-128, AS350, Cessna 208", "detail": "Tiny; coastal patrol; ECOWAS"},
    {"country": "TG", "name": "Togolese Air Force", "total_aircraft": 8, "fighters": 0, "bombers": 0, "attack": 4, "transport": 2, "helicopters": 2, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Alpha Jet, EMB-326GB, AS350", "detail": "Tiny; limited Alpha Jet airframes"},

    # ── Africa (Central / Southern) ──
    {"country": "CD", "name": "Democratic Republic of Congo Air Force (FAC)", "total_aircraft": 32, "fighters": 4, "bombers": 0, "attack": 6, "transport": 8, "helicopters": 14, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Su-25 (limited), MiG-23 (storage), Mi-24, Mi-8, An-26, L-39", "detail": "Mostly grounded; M23 conflict; UN MONUSCO winding down"},
    {"country": "CF", "name": "Central African Air Force", "total_aircraft": 4, "fighters": 0, "bombers": 0, "attack": 0, "transport": 2, "helicopters": 2, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Cessna 337, Mi-8 (limited)", "detail": "Effectively non-existent; Wagner support; civil war legacy"},
    {"country": "GQ", "name": "Equatorial Guinea Air Force", "total_aircraft": 10, "fighters": 4, "bombers": 0, "attack": 2, "transport": 2, "helicopters": 2, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Su-25 (4 Ukrainian), An-72, Mi-24, AW139", "detail": "Oil-funded; Russian/Ukrainian/Israeli mix; coup attempt 2024"},
    {"country": "ZM", "name": "Zambian Air Force", "total_aircraft": 70, "fighters": 18, "bombers": 0, "attack": 10, "transport": 12, "helicopters": 24, "tankers": 0, "special_mission": 6, "awacs": 0, "key_types": "K-8 Karakorum, MB-326 (retired), Mi-17, MA60, Y-12", "detail": "Chinese-supplied; K-8 trainer/light attack"},
    {"country": "ZW", "name": "Air Force of Zimbabwe", "total_aircraft": 50, "fighters": 8, "bombers": 0, "attack": 8, "transport": 6, "helicopters": 22, "tankers": 0, "special_mission": 6, "awacs": 0, "key_types": "Chengdu F-7 (J-7), Hawk Mk60, K-8, Mi-35P, AB-412", "detail": "Sanctions-impacted; Chinese spares; F-7 + Hawk legacy"},
    {"country": "MZ", "name": "Mozambique Defence Armed Forces Air Wing", "total_aircraft": 20, "fighters": 0, "bombers": 0, "attack": 4, "transport": 4, "helicopters": 12, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Mi-24, Mi-17, An-26, Cessna 182, Bayraktar TB2", "detail": "Cabo Delgado insurgency; SAMIM/Rwandan support; TB2 acquired 2024"},
    {"country": "MG", "name": "Malagasy Air Force", "total_aircraft": 12, "fighters": 0, "bombers": 0, "attack": 0, "transport": 4, "helicopters": 8, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "An-26, Cessna 337, Mi-8", "detail": "Coastal patrol; piracy response"},
    {"country": "NA", "name": "Namibian Air Force", "total_aircraft": 18, "fighters": 4, "bombers": 0, "attack": 0, "transport": 4, "helicopters": 10, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Chengdu F-7NM, K-8, Mi-25, Cheetah, Cessna 337", "detail": "Chinese-supplied; F-7 fighters"},
    {"country": "BW", "name": "Botswana Defence Force Air Wing", "total_aircraft": 40, "fighters": 13, "bombers": 0, "attack": 0, "transport": 8, "helicopters": 14, "tankers": 0, "special_mission": 5, "awacs": 0, "key_types": "F-5A/B Tigershark, BD-700 Global Express, PC-7, Bell 412, AS350", "detail": "F-5 fleet aging; Saab Gripen acquisition rumored"},

    # ── Latin America (additional) ──
    {"country": "CU", "name": "Cuban Revolutionary Air and Air Defence Force", "total_aircraft": 96, "fighters": 16, "bombers": 0, "attack": 8, "transport": 12, "helicopters": 50, "tankers": 0, "special_mission": 10, "awacs": 0, "key_types": "MiG-29 (4), MiG-23ML, MiG-21 (storage), Mi-8/17, Mi-35", "detail": "Mostly grounded; sanctions-driven cannibalization; Russian-vintage"},
    {"country": "PY", "name": "Paraguayan Air Force", "total_aircraft": 30, "fighters": 0, "bombers": 0, "attack": 4, "transport": 6, "helicopters": 12, "tankers": 0, "special_mission": 8, "awacs": 0, "key_types": "EMB-312 Tucano, A-37B (retired), Bell 205, Cessna 402, T-35", "detail": "Tiny; counter-narcotics"},
    {"country": "HN", "name": "Honduran Air Force (FAH)", "total_aircraft": 60, "fighters": 8, "bombers": 0, "attack": 6, "transport": 10, "helicopters": 30, "tankers": 0, "special_mission": 6, "awacs": 0, "key_types": "F-5E Tiger II, A-37B, EMB-312 Tucano, Bell UH-1H, MD-500", "detail": "F-5 oldest in service worldwide; counter-narcotics"},
    {"country": "NI", "name": "Nicaraguan Air Force (FAN)", "total_aircraft": 15, "fighters": 0, "bombers": 0, "attack": 4, "transport": 4, "helicopters": 7, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Mi-17, Mi-24 (some), An-26, Cessna 337", "detail": "Russian-aligned; small force"},
    {"country": "SV", "name": "Salvadoran Air Force (FAS)", "total_aircraft": 30, "fighters": 0, "bombers": 0, "attack": 8, "transport": 6, "helicopters": 12, "tankers": 0, "special_mission": 4, "awacs": 0, "key_types": "A-37B Dragonfly, OA-37, MD-500, Bell UH-1H, Cessna 210", "detail": "A-37 oldest in service; gang interdiction"},
    {"country": "JM", "name": "Jamaica Defence Force Air Wing", "total_aircraft": 16, "fighters": 0, "bombers": 0, "attack": 0, "transport": 4, "helicopters": 12, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Bell 206/407/412, AS355, BN-2 Islander, DA-42", "detail": "Coastal patrol; counter-narcotics; no combat aircraft"},
    {"country": "BS", "name": "Royal Bahamas Defence Force Air Wing", "total_aircraft": 6, "fighters": 0, "bombers": 0, "attack": 0, "transport": 2, "helicopters": 4, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "AW139, BN-2T Islander", "detail": "Maritime patrol; drug interdiction with US"},
    {"country": "GY", "name": "Guyana Defence Force Air Corps", "total_aircraft": 8, "fighters": 0, "bombers": 0, "attack": 0, "transport": 3, "helicopters": 5, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Bell 206/412, Y-12, Britten-Norman Islander", "detail": "Tiny; Venezuela border tension; oil boom funding modernization"},
    {"country": "TT", "name": "Trinidad and Tobago Air Guard", "total_aircraft": 7, "fighters": 0, "bombers": 0, "attack": 0, "transport": 2, "helicopters": 5, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "AW139, S-76, C-26 Metroliner", "detail": "Maritime patrol; oil platform security"},

    # ── Europe (additional) ──
    {"country": "AL", "name": "Albanian Air Force", "total_aircraft": 19, "fighters": 0, "bombers": 0, "attack": 0, "transport": 4, "helicopters": 15, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "AB-205, AB-206, BO 105, Eurocopter Cougar, Bell 222", "detail": "NATO; no combat aircraft since MiG retirement; helicopter-only"},
    {"country": "BA", "name": "Armed Forces of Bosnia and Herzegovina Air Force", "total_aircraft": 24, "fighters": 0, "bombers": 0, "attack": 4, "transport": 4, "helicopters": 16, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "J-22 Orao (retired), Mi-8/17, UH-1H, Bell 205, Gazelle", "detail": "NATO partner; Orao grounded; helicopter focus"},
    {"country": "MK", "name": "North Macedonia Air Brigade", "total_aircraft": 18, "fighters": 0, "bombers": 0, "attack": 0, "transport": 4, "helicopters": 14, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Mi-8/17, Mi-24V (retired), UH-60M Black Hawk (incoming), Zlin 242", "detail": "NATO since 2020; UH-60M acquisition; transitioning to Western kit"},
    {"country": "ME", "name": "Air Force of Montenegro", "total_aircraft": 8, "fighters": 0, "bombers": 0, "attack": 0, "transport": 2, "helicopters": 6, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Bell 412, Gazelle, G-4 Super Galeb (retired)", "detail": "NATO since 2017; tiny; no combat aircraft"},
    {"country": "XK", "name": "Kosovo Security Force Air Element", "total_aircraft": 4, "fighters": 0, "bombers": 0, "attack": 0, "transport": 0, "helicopters": 4, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Bayraktar TB2 (5 ordered), Bell 206 (limited)", "detail": "Tiny; KFOR-supported; TB2 acquired 2023 amid Serbia tensions"},
    {"country": "CY", "name": "Cyprus Air Forces Command", "total_aircraft": 16, "fighters": 0, "bombers": 0, "attack": 4, "transport": 2, "helicopters": 10, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "SA342L Gazelle (HOT missile), Bell 206, AW139, Mi-35P (retired), PC-9", "detail": "EU non-NATO; HOT-armed Gazelles; defense pact with France/Greece"},
    {"country": "MT", "name": "Armed Forces of Malta Air Wing", "total_aircraft": 11, "fighters": 0, "bombers": 0, "attack": 0, "transport": 4, "helicopters": 7, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "AW139, BN-2 Islander, King Air 200, AS350", "detail": "Maritime SAR; migrant interdiction; EU non-NATO"},
    {"country": "IE", "name": "Irish Air Corps", "total_aircraft": 27, "fighters": 0, "bombers": 0, "attack": 0, "transport": 6, "helicopters": 15, "tankers": 0, "special_mission": 6, "awacs": 0, "key_types": "PC-9M, PC-12, CN-235MPA, AW139, EC135, Britten-Norman Defender", "detail": "Neutral; no fighters; UK-RAF QRA covers Irish airspace; PC-12 acquisition planned"},
    {"country": "LU", "name": "Luxembourg Army Aviation (NATO contribution)", "total_aircraft": 19, "fighters": 0, "bombers": 0, "attack": 0, "transport": 4, "helicopters": 1, "tankers": 0, "special_mission": 14, "awacs": 0, "key_types": "A330 MRTT (NATO MMF), A400M, NH90, Bombardier Global 7500 (planned)", "detail": "Hosts NATO MMF MRTT pool with Netherlands/Germany; no combat aircraft"},
    {"country": "MD", "name": "Moldovan Air Force", "total_aircraft": 9, "fighters": 0, "bombers": 0, "attack": 0, "transport": 2, "helicopters": 7, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "Mi-8, An-2, An-26 (retired)", "detail": "Tiny; no combat aircraft; neutral but Western-leaning post-Ukraine war"},
    {"country": "PS", "name": "Palestinian Civil Aviation Authority (no air force)", "total_aircraft": 2, "fighters": 0, "bombers": 0, "attack": 0, "transport": 0, "helicopters": 2, "tankers": 0, "special_mission": 0, "awacs": 0, "key_types": "VIP helicopters (Mi-8 historic, mostly destroyed)", "detail": "No military air force; Gaza war devastated remaining assets"},
]


# ═══════════════ GEOPOLITICAL INDICES (per country) ═══════════════
# Sources: Economist Intelligence Unit, Transparency Intl, RSF, UNDP, SIPRI, World Bank
# Scores: democracy_index (0-10), corruption_cpi (0-100), press_freedom (0=best,100=worst),
#         hdi (0-1), military_spend_pct (% of GDP), gdp_growth (%), fragile_state (0-120, high=worse)
COUNTRY_INDICES = {
    # ── Major powers ──
    "US": {"democracy_index": 7.85, "corruption_cpi": 69, "press_freedom": 26.7, "hdi": 0.927, "military_spend_pct": 3.4, "gdp_growth": 2.5, "fragile_state": 38.2},
    "CN": {"democracy_index": 1.94, "corruption_cpi": 42, "press_freedom": 83.5, "hdi": 0.788, "military_spend_pct": 1.7, "gdp_growth": 5.0, "fragile_state": 68.7},
    "RU": {"democracy_index": 2.22, "corruption_cpi": 26, "press_freedom": 76.1, "hdi": 0.822, "military_spend_pct": 5.9, "gdp_growth": 3.6, "fragile_state": 72.5},
    "GB": {"democracy_index": 8.54, "corruption_cpi": 71, "press_freedom": 23.1, "hdi": 0.929, "military_spend_pct": 2.3, "gdp_growth": 0.7, "fragile_state": 30.5},
    "FR": {"democracy_index": 8.07, "corruption_cpi": 72, "press_freedom": 21.4, "hdi": 0.903, "military_spend_pct": 2.1, "gdp_growth": 1.1, "fragile_state": 30.2},
    "DE": {"democracy_index": 8.80, "corruption_cpi": 78, "press_freedom": 17.2, "hdi": 0.942, "military_spend_pct": 1.6, "gdp_growth": -0.2, "fragile_state": 25.3},
    "JP": {"democracy_index": 8.33, "corruption_cpi": 73, "press_freedom": 32.4, "hdi": 0.920, "military_spend_pct": 1.2, "gdp_growth": 1.9, "fragile_state": 28.0},
    "IN": {"democracy_index": 7.18, "corruption_cpi": 39, "press_freedom": 53.6, "hdi": 0.644, "military_spend_pct": 2.4, "gdp_growth": 7.8, "fragile_state": 70.2},
    # ── Europe ──
    "IT": {"democracy_index": 7.69, "corruption_cpi": 56, "press_freedom": 26.4, "hdi": 0.895, "military_spend_pct": 1.5, "gdp_growth": 0.9, "fragile_state": 33.5},
    "ES": {"democracy_index": 8.07, "corruption_cpi": 60, "press_freedom": 20.6, "hdi": 0.905, "military_spend_pct": 1.3, "gdp_growth": 2.5, "fragile_state": 30.0},
    "PL": {"democracy_index": 7.04, "corruption_cpi": 54, "press_freedom": 28.4, "hdi": 0.876, "military_spend_pct": 4.0, "gdp_growth": 3.5, "fragile_state": 32.5},
    "NL": {"democracy_index": 9.00, "corruption_cpi": 79, "press_freedom": 10.8, "hdi": 0.941, "military_spend_pct": 1.8, "gdp_growth": 0.6, "fragile_state": 22.0},
    "SE": {"democracy_index": 9.39, "corruption_cpi": 83, "press_freedom": 11.2, "hdi": 0.947, "military_spend_pct": 2.2, "gdp_growth": -0.1, "fragile_state": 18.5},
    "NO": {"democracy_index": 9.81, "corruption_cpi": 84, "press_freedom": 7.3, "hdi": 0.961, "military_spend_pct": 2.0, "gdp_growth": 0.5, "fragile_state": 17.0},
    "DK": {"democracy_index": 9.28, "corruption_cpi": 90, "press_freedom": 8.6, "hdi": 0.948, "military_spend_pct": 2.4, "gdp_growth": 1.8, "fragile_state": 18.0},
    "FI": {"democracy_index": 9.30, "corruption_cpi": 87, "press_freedom": 5.2, "hdi": 0.940, "military_spend_pct": 2.4, "gdp_growth": -1.0, "fragile_state": 16.5},
    "CH": {"democracy_index": 9.14, "corruption_cpi": 82, "press_freedom": 12.0, "hdi": 0.962, "military_spend_pct": 0.8, "gdp_growth": 0.7, "fragile_state": 15.0},
    "AT": {"democracy_index": 8.41, "corruption_cpi": 71, "press_freedom": 17.5, "hdi": 0.916, "military_spend_pct": 0.8, "gdp_growth": -0.7, "fragile_state": 22.5},
    "BE": {"democracy_index": 7.64, "corruption_cpi": 73, "press_freedom": 14.8, "hdi": 0.931, "military_spend_pct": 1.3, "gdp_growth": 1.3, "fragile_state": 25.0},
    "IE": {"democracy_index": 9.19, "corruption_cpi": 77, "press_freedom": 13.0, "hdi": 0.945, "military_spend_pct": 0.2, "gdp_growth": -3.2, "fragile_state": 19.5},
    "PT": {"democracy_index": 8.03, "corruption_cpi": 61, "press_freedom": 14.6, "hdi": 0.866, "military_spend_pct": 1.5, "gdp_growth": 2.3, "fragile_state": 28.0},
    "GR": {"democracy_index": 7.97, "corruption_cpi": 49, "press_freedom": 36.3, "hdi": 0.887, "military_spend_pct": 3.0, "gdp_growth": 2.0, "fragile_state": 38.5},
    "CZ": {"democracy_index": 7.97, "corruption_cpi": 57, "press_freedom": 18.1, "hdi": 0.889, "military_spend_pct": 2.1, "gdp_growth": -0.4, "fragile_state": 28.0},
    "HU": {"democracy_index": 6.64, "corruption_cpi": 42, "press_freedom": 37.8, "hdi": 0.849, "military_spend_pct": 2.4, "gdp_growth": -0.9, "fragile_state": 45.5},
    "RO": {"democracy_index": 6.45, "corruption_cpi": 46, "press_freedom": 28.2, "hdi": 0.821, "military_spend_pct": 2.4, "gdp_growth": 2.1, "fragile_state": 42.0},
    "UA": {"democracy_index": 5.42, "corruption_cpi": 36, "press_freedom": 40.6, "hdi": 0.773, "military_spend_pct": 37.0, "gdp_growth": 5.3, "fragile_state": 78.5},
    "RS": {"democracy_index": 6.36, "corruption_cpi": 36, "press_freedom": 36.2, "hdi": 0.802, "military_spend_pct": 2.4, "gdp_growth": 2.5, "fragile_state": 55.0},
    "BY": {"democracy_index": 2.41, "corruption_cpi": 39, "press_freedom": 79.2, "hdi": 0.808, "military_spend_pct": 1.3, "gdp_growth": 3.9, "fragile_state": 68.0},
    # ── Americas ──
    "CA": {"democracy_index": 9.22, "corruption_cpi": 76, "press_freedom": 15.2, "hdi": 0.935, "military_spend_pct": 1.4, "gdp_growth": 1.1, "fragile_state": 22.0},
    "BR": {"democracy_index": 6.68, "corruption_cpi": 36, "press_freedom": 27.5, "hdi": 0.760, "military_spend_pct": 1.1, "gdp_growth": 2.9, "fragile_state": 56.0},
    "MX": {"democracy_index": 5.87, "corruption_cpi": 31, "press_freedom": 56.4, "hdi": 0.781, "military_spend_pct": 0.6, "gdp_growth": 3.2, "fragile_state": 62.5},
    "AR": {"democracy_index": 6.84, "corruption_cpi": 37, "press_freedom": 24.1, "hdi": 0.842, "military_spend_pct": 0.5, "gdp_growth": -1.6, "fragile_state": 44.0},
    "CL": {"democracy_index": 8.22, "corruption_cpi": 66, "press_freedom": 17.8, "hdi": 0.855, "military_spend_pct": 2.0, "gdp_growth": 0.2, "fragile_state": 34.0},
    "CO": {"democracy_index": 7.04, "corruption_cpi": 39, "press_freedom": 33.7, "hdi": 0.758, "military_spend_pct": 3.1, "gdp_growth": 0.6, "fragile_state": 62.0},
    "VE": {"democracy_index": 2.28, "corruption_cpi": 13, "press_freedom": 55.2, "hdi": 0.691, "military_spend_pct": 0.5, "gdp_growth": 4.0, "fragile_state": 82.5},
    "CU": {"democracy_index": 2.84, "corruption_cpi": 42, "press_freedom": 71.2, "hdi": 0.764, "military_spend_pct": 2.9, "gdp_growth": 1.8, "fragile_state": 55.0},
    # ── Middle East ──
    "IL": {"democracy_index": 7.84, "corruption_cpi": 62, "press_freedom": 32.4, "hdi": 0.919, "military_spend_pct": 5.3, "gdp_growth": 2.0, "fragile_state": 55.5},
    "IR": {"democracy_index": 1.96, "corruption_cpi": 24, "press_freedom": 78.5, "hdi": 0.774, "military_spend_pct": 2.4, "gdp_growth": 5.4, "fragile_state": 75.5},
    "SA": {"democracy_index": 2.08, "corruption_cpi": 52, "press_freedom": 67.2, "hdi": 0.875, "military_spend_pct": 7.1, "gdp_growth": -0.8, "fragile_state": 60.0},
    "AE": {"democracy_index": 2.76, "corruption_cpi": 68, "press_freedom": 51.4, "hdi": 0.911, "military_spend_pct": 4.4, "gdp_growth": 3.1, "fragile_state": 40.0},
    "TR": {"democracy_index": 4.35, "corruption_cpi": 34, "press_freedom": 54.7, "hdi": 0.838, "military_spend_pct": 1.6, "gdp_growth": 4.5, "fragile_state": 65.5},
    "EG": {"democracy_index": 2.93, "corruption_cpi": 30, "press_freedom": 56.8, "hdi": 0.731, "military_spend_pct": 1.2, "gdp_growth": 3.8, "fragile_state": 72.0},
    "IQ": {"democracy_index": 3.62, "corruption_cpi": 23, "press_freedom": 52.6, "hdi": 0.686, "military_spend_pct": 2.5, "gdp_growth": -2.3, "fragile_state": 85.5},
    "SY": {"democracy_index": 1.43, "corruption_cpi": 13, "press_freedom": 68.4, "hdi": 0.577, "military_spend_pct": 2.0, "gdp_growth": 3.0, "fragile_state": 102.0},
    "YE": {"democracy_index": 1.95, "corruption_cpi": 16, "press_freedom": 72.5, "hdi": 0.455, "military_spend_pct": 4.0, "gdp_growth": -2.0, "fragile_state": 108.5},
    "PS": {"democracy_index": 3.86, "corruption_cpi": 24, "press_freedom": 45.2, "hdi": 0.716, "military_spend_pct": 0.0, "gdp_growth": -10.0, "fragile_state": 90.5},
    # ── Asia ──
    "KR": {"democracy_index": 8.09, "corruption_cpi": 63, "press_freedom": 28.5, "hdi": 0.929, "military_spend_pct": 2.8, "gdp_growth": 1.4, "fragile_state": 32.0},
    "KP": {"democracy_index": 1.08, "corruption_cpi": 17, "press_freedom": 87.2, "hdi": 0.733, "military_spend_pct": 24.0, "gdp_growth": 1.0, "fragile_state": 88.5},
    "TW": {"democracy_index": 8.99, "corruption_cpi": 67, "press_freedom": 14.8, "hdi": 0.926, "military_spend_pct": 2.5, "gdp_growth": 1.3, "fragile_state": 28.0},
    "PK": {"democracy_index": 4.31, "corruption_cpi": 29, "press_freedom": 52.0, "hdi": 0.544, "military_spend_pct": 3.7, "gdp_growth": -0.2, "fragile_state": 90.0},
    "BD": {"democracy_index": 5.99, "corruption_cpi": 24, "press_freedom": 53.6, "hdi": 0.670, "military_spend_pct": 1.3, "gdp_growth": 5.8, "fragile_state": 80.5},
    "TH": {"democracy_index": 6.35, "corruption_cpi": 35, "press_freedom": 42.5, "hdi": 0.800, "military_spend_pct": 1.3, "gdp_growth": 1.9, "fragile_state": 60.0},
    "VN": {"democracy_index": 2.73, "corruption_cpi": 41, "press_freedom": 78.0, "hdi": 0.726, "military_spend_pct": 2.3, "gdp_growth": 5.0, "fragile_state": 55.0},
    "PH": {"democracy_index": 6.73, "corruption_cpi": 34, "press_freedom": 44.0, "hdi": 0.710, "military_spend_pct": 1.1, "gdp_growth": 5.6, "fragile_state": 65.5},
    "ID": {"democracy_index": 6.71, "corruption_cpi": 34, "press_freedom": 38.5, "hdi": 0.713, "military_spend_pct": 0.8, "gdp_growth": 5.1, "fragile_state": 62.0},
    "MY": {"democracy_index": 7.30, "corruption_cpi": 50, "press_freedom": 38.2, "hdi": 0.803, "military_spend_pct": 1.0, "gdp_growth": 3.7, "fragile_state": 48.0},
    "SG": {"democracy_index": 6.02, "corruption_cpi": 83, "press_freedom": 48.2, "hdi": 0.939, "military_spend_pct": 3.0, "gdp_growth": 1.1, "fragile_state": 26.0},
    "MM": {"democracy_index": 1.74, "corruption_cpi": 20, "press_freedom": 74.5, "hdi": 0.585, "military_spend_pct": 3.0, "gdp_growth": 1.0, "fragile_state": 96.5},
    "AF": {"democracy_index": 0.32, "corruption_cpi": 20, "press_freedom": 62.4, "hdi": 0.462, "military_spend_pct": 3.3, "gdp_growth": 3.0, "fragile_state": 106.0},
    "KZ": {"democracy_index": 2.82, "corruption_cpi": 39, "press_freedom": 55.2, "hdi": 0.811, "military_spend_pct": 0.7, "gdp_growth": 5.1, "fragile_state": 55.0},
    "MN": {"democracy_index": 6.48, "corruption_cpi": 33, "press_freedom": 22.0, "hdi": 0.741, "military_spend_pct": 0.6, "gdp_growth": 7.0, "fragile_state": 55.5},
    # ── Africa ──
    "ZA": {"democracy_index": 7.05, "corruption_cpi": 43, "press_freedom": 20.0, "hdi": 0.713, "military_spend_pct": 0.7, "gdp_growth": 0.7, "fragile_state": 62.5},
    "NG": {"democracy_index": 4.29, "corruption_cpi": 25, "press_freedom": 45.8, "hdi": 0.539, "military_spend_pct": 0.7, "gdp_growth": 2.9, "fragile_state": 88.0},
    "KE": {"democracy_index": 5.31, "corruption_cpi": 31, "press_freedom": 35.2, "hdi": 0.575, "military_spend_pct": 1.2, "gdp_growth": 5.6, "fragile_state": 78.5},
    "ET": {"democracy_index": 3.27, "corruption_cpi": 37, "press_freedom": 45.0, "hdi": 0.492, "military_spend_pct": 0.7, "gdp_growth": 7.2, "fragile_state": 92.5},
    "GH": {"democracy_index": 6.64, "corruption_cpi": 43, "press_freedom": 23.5, "hdi": 0.602, "military_spend_pct": 0.4, "gdp_growth": 3.1, "fragile_state": 62.0},
    "DZ": {"democracy_index": 3.66, "corruption_cpi": 33, "press_freedom": 58.5, "hdi": 0.745, "military_spend_pct": 9.8, "gdp_growth": 4.1, "fragile_state": 68.0},
    "MA": {"democracy_index": 5.04, "corruption_cpi": 38, "press_freedom": 52.1, "hdi": 0.683, "military_spend_pct": 4.0, "gdp_growth": 3.4, "fragile_state": 58.5},
    "SD": {"democracy_index": 2.47, "corruption_cpi": 20, "press_freedom": 64.5, "hdi": 0.516, "military_spend_pct": 1.1, "gdp_growth": -12.0, "fragile_state": 106.5},
    "CD": {"democracy_index": 1.81, "corruption_cpi": 20, "press_freedom": 52.0, "hdi": 0.479, "military_spend_pct": 0.7, "gdp_growth": 6.2, "fragile_state": 108.0},
    "SS": {"democracy_index": 1.41, "corruption_cpi": 13, "press_freedom": 66.0, "hdi": 0.385, "military_spend_pct": 2.3, "gdp_growth": -0.3, "fragile_state": 110.0},
    "RW": {"democracy_index": 3.16, "corruption_cpi": 53, "press_freedom": 58.4, "hdi": 0.548, "military_spend_pct": 1.4, "gdp_growth": 8.2, "fragile_state": 70.0},
    # ── Oceania ──
    "AU": {"democracy_index": 8.71, "corruption_cpi": 75, "press_freedom": 16.2, "hdi": 0.946, "military_spend_pct": 2.0, "gdp_growth": 2.0, "fragile_state": 22.0},
    "NZ": {"democracy_index": 9.61, "corruption_cpi": 85, "press_freedom": 8.5, "hdi": 0.931, "military_spend_pct": 1.5, "gdp_growth": 0.6, "fragile_state": 18.0},
}


# ═══════════════ ALLIANCE BLOCS ═══════════════
ALLIANCE_BLOCS = [
    {
        "name": "NATO", "founded": 1949, "members": 32,
        "member_codes": ["US","CA","GB","FR","DE","IT","ES","PL","NL","BE","TR","NO","DK","PT","GR","CZ","HU","BG","RO","SK","HR","SI","LT","LV","EE","AL","MK","ME","IS","LU","FI","SE"],
        "combined_gdp": "$42.2T", "combined_military": "3.3M active",
        "combined_nukes": 5649, "purpose": "Collective defense (Article 5)",
        "trend": "expanding", "trend_detail": "Finland (2023) & Sweden (2024) joined. 23+ members at 2% GDP defense target.",
        "hq": "Brussels",
    },
    {
        "name": "BRICS", "founded": 2009, "members": 10,
        "member_codes": ["BR","RU","IN","CN","ZA","EG","ET","IR","AE","ID"],
        "combined_gdp": "$32.1T", "combined_military": "5.5M active",
        "combined_nukes": 6342, "purpose": "Multipolar economic cooperation; alternative to Western-led order",
        "trend": "rapidly expanding", "trend_detail": "5 new members joined Jan 2024. 30+ countries expressed interest. New Development Bank as IMF alternative.",
        "hq": "Rotating presidency",
    },
    {
        "name": "EU", "founded": 1993, "members": 27,
        "member_codes": ["DE","FR","IT","ES","PL","NL","BE","AT","SE","DK","FI","IE","PT","GR","CZ","HU","RO","BG","SK","HR","SI","LT","LV","EE","MT","CY","LU"],
        "combined_gdp": "$18.4T", "combined_military": "1.3M active",
        "combined_nukes": 515, "purpose": "Political & economic union; single market",
        "trend": "consolidating", "trend_detail": "Ukraine, Moldova, Georgia candidate status. Post-Brexit adjustment. Green Deal & defense autonomy push.",
        "hq": "Brussels/Strasbourg",
    },
    {
        "name": "SCO", "founded": 2001, "members": 10,
        "member_codes": ["CN","RU","IN","PK","KZ","UZ","KG","TJ","IR","BY"],
        "combined_gdp": "$24.8T", "combined_military": "4.2M active",
        "combined_nukes": 6132, "purpose": "Security & economic cooperation; counter-terrorism",
        "trend": "expanding", "trend_detail": "Belarus joined 2024. 16 dialogue partners. Growing defense cooperation exercises.",
        "hq": "Beijing",
    },
    {
        "name": "African Union", "founded": 2002, "members": 55,
        "member_codes": ["EG","NG","ZA","KE","ET","DZ","MA","GH","SN","RW","TZ","UG","CM","CI","AO","MZ","SD","CD","TN","LY"],
        "combined_gdp": "$2.97T", "combined_military": "2.1M active",
        "combined_nukes": 0, "purpose": "Continental unity; peace & development",
        "trend": "reforming", "trend_detail": "Agenda 2063. AfCFTA largest free trade area by members. G20 seat gained 2023.",
        "hq": "Addis Ababa",
    },
    {
        "name": "ASEAN", "founded": 1967, "members": 10,
        "member_codes": ["ID","TH","VN","PH","MY","SG","MM","KH","LA","BN"],
        "combined_gdp": "$3.66T", "combined_military": "2.7M active",
        "combined_nukes": 0, "purpose": "Regional stability & economic integration; centrality principle",
        "trend": "stable", "trend_detail": "Timor-Leste accession in progress. RCEP launched. Struggling with Myanmar crisis & South China Sea.",
        "hq": "Jakarta",
    },
    {
        "name": "AUKUS", "founded": 2021, "members": 3,
        "member_codes": ["AU","GB","US"],
        "combined_gdp": "$32.5T", "combined_military": "1.7M active",
        "combined_nukes": 5269, "purpose": "Indo-Pacific security; nuclear submarine tech transfer",
        "trend": "deepening", "trend_detail": "Pillar I: SSN-AUKUS submarines. Pillar II: AI, quantum, hypersonics. Japan/Canada/NZ potential Pillar II.",
        "hq": "No fixed HQ",
    },
    {
        "name": "Quad", "founded": 2007, "members": 4,
        "member_codes": ["US","JP","AU","IN"],
        "combined_gdp": "$39.8T", "combined_military": "3.1M active",
        "combined_nukes": 5216, "purpose": "Free & open Indo-Pacific; counter-China coordination",
        "trend": "maturing", "trend_detail": "Annual summits since 2021. Vaccine diplomacy, maritime awareness, supply chain initiatives.",
        "hq": "No fixed HQ",
    },
    {
        "name": "Five Eyes", "founded": 1941, "members": 5,
        "member_codes": ["US","GB","CA","AU","NZ"],
        "combined_gdp": "$33.8T", "combined_military": "1.8M active",
        "combined_nukes": 5269, "purpose": "Signals intelligence alliance",
        "trend": "stable", "trend_detail": "World's oldest intelligence-sharing alliance. Expanded to cyber, counter-terrorism, technology.",
        "hq": "No fixed HQ (rotating)",
    },
    {
        "name": "G7", "founded": 1975, "members": 7,
        "member_codes": ["US","GB","FR","DE","IT","JP","CA"],
        "combined_gdp": "$46.3T", "combined_military": "2.5M active",
        "combined_nukes": 5649, "purpose": "Advanced economy coordination; rules-based order",
        "trend": "refocusing", "trend_detail": "Ukraine support coordination. China de-risking strategy. AI governance. Shrinking share of global GDP (29%).",
        "hq": "Rotating presidency",
    },
    {
        "name": "G20", "founded": 1999, "members": 20,
        "member_codes": ["US","CN","JP","DE","GB","IN","FR","IT","BR","CA","KR","AU","MX","ID","SA","TR","AR","ZA","RU","EU"],
        "combined_gdp": "$91.8T", "combined_military": "12M active",
        "combined_nukes": 12300, "purpose": "Global economic governance; 85% of world GDP",
        "trend": "fractured", "trend_detail": "AU added as permanent member. Divisions over Ukraine/Gaza. Debt relief stalled. Reform calls growing.",
        "hq": "Rotating presidency",
    },
    {
        "name": "GCC", "founded": 1981, "members": 6,
        "member_codes": ["SA","AE","QA","BH","KW","OM"],
        "combined_gdp": "$2.1T", "combined_military": "460K active",
        "combined_nukes": 0, "purpose": "Gulf cooperation; economic & security integration",
        "trend": "diversifying", "trend_detail": "Vision 2030 & post-oil transitions. Diplomatic normalization with Israel (Abraham Accords). De-dollarization discussions.",
        "hq": "Riyadh",
    },
    {
        "name": "CSTO", "founded": 2002, "members": 6,
        "member_codes": ["RU","BY","KZ","KG","TJ","AM"],
        "combined_gdp": "$2.4T", "combined_military": "1.4M active",
        "combined_nukes": 5580, "purpose": "Russian-led collective security",
        "trend": "weakening", "trend_detail": "Armenia frozen participation 2024. Kazakhstan distancing. Credibility questioned after Nagorno-Karabakh.",
        "hq": "Moscow",
    },
]


# ═══════════════ GLOBAL TRENDS ═══════════════
# Yearly data points for key geopolitical indicators
GLOBAL_TRENDS = {
    "democracy": {
        "title": "Global Democracy", "unit": "countries",
        "description": "Number of full democracies (EIU Democracy Index ≥8.0)",
        "years": [2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025],
        "values": [20, 19, 19, 20, 22, 23, 21, 24, 24, 23, 22],
        "trend": "declining", "color": "#22d3ee",
    },
    "autocracies": {
        "title": "Authoritarian Regimes", "unit": "countries",
        "description": "Number of authoritarian regimes (EIU <4.0)",
        "years": [2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025],
        "values": [51, 51, 52, 52, 54, 57, 59, 59, 59, 60, 61],
        "trend": "rising", "color": "#dc2626",
    },
    "armed_conflicts": {
        "title": "Armed Conflicts", "unit": "active",
        "description": "State-based armed conflicts globally (UCDP)",
        "years": [2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025],
        "values": [52, 53, 49, 52, 54, 56, 54, 55, 59, 56, 55],
        "trend": "elevated", "color": "#ea580c",
    },
    "battle_deaths": {
        "title": "Battle Deaths", "unit": "thousands/yr",
        "description": "Estimated annual battle-related deaths (UCDP/PRIO)",
        "years": [2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025],
        "values": [97, 87, 69, 54, 48, 42, 48, 84, 122, 105, 90],
        "trend": "elevated", "color": "#dc2626",
    },
    "displaced_persons": {
        "title": "Forcibly Displaced", "unit": "million",
        "description": "Refugees + IDPs + asylum seekers globally (UNHCR)",
        "years": [2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025],
        "values": [65, 66, 69, 71, 80, 82, 89, 100, 110, 117, 120],
        "trend": "rising", "color": "#f97316",
    },
    "military_spending": {
        "title": "Global Mil. Spending", "unit": "$T/yr",
        "description": "Total worldwide military expenditure (SIPRI)",
        "years": [2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025],
        "values": [1.68, 1.69, 1.73, 1.79, 1.85, 1.92, 1.99, 2.11, 2.24, 2.44, 2.60],
        "trend": "rising", "color": "#ef4444",
    },
    "nuclear_warheads": {
        "title": "Nuclear Warheads", "unit": "total stockpile",
        "description": "Estimated global nuclear warhead inventory (FAS/SIPRI)",
        "years": [2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025],
        "values": [15395, 15350, 14935, 14465, 13890, 13400, 13150, 12705, 12512, 12121, 12100],
        "trend": "declining but modernizing", "color": "#a855f7",
    },
    "global_trade": {
        "title": "Global Trade Vol.", "unit": "$T/yr",
        "description": "World merchandise trade (WTO)",
        "years": [2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025],
        "values": [16.5, 16.0, 17.7, 19.5, 19.0, 17.6, 22.3, 25.3, 24.0, 25.0, 25.8],
        "trend": "recovering", "color": "#16a34a",
    },
    "press_freedom": {
        "title": "Press Freedom", "unit": "# countries bad/v.bad",
        "description": "Countries rated 'difficult' or worse by RSF",
        "years": [2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025],
        "values": [52, 54, 56, 58, 59, 62, 64, 67, 70, 72, 73],
        "trend": "worsening", "color": "#ca8a04",
    },
    "sanctions_regimes": {
        "title": "Active Sanctions", "unit": "country programs",
        "description": "Major US/EU/UN sanctions programs active",
        "years": [2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025],
        "values": [18, 20, 22, 26, 28, 29, 30, 38, 40, 42, 44],
        "trend": "rising sharply", "color": "#f97316",
    },
}


# ═══════════════ DEMOCRACY TRACKER ═══════════════
# Classification of all 167 EIU-scored countries (2024 data)
DEMOCRACY_TRACKER = {
    "full_democracies": {
        "count": 22, "label": "Full Democracies", "color": "#16a34a",
        "description": "Score 8.0-10.0: strong institutions, free press, independent judiciary",
        "examples": ["NO", "NZ", "FI", "SE", "DK", "IE", "CH", "NL", "TW", "CA", "AU", "DE", "GB"],
        "population_pct": 5.7,
    },
    "flawed_democracies": {
        "count": 54, "label": "Flawed Democracies", "color": "#65a30d",
        "description": "Score 6.0-7.99: elections fair but governance/media issues",
        "examples": ["US", "FR", "IT", "JP", "BR", "IN", "ZA", "MX", "KR", "CO", "PL", "AR", "PH"],
        "population_pct": 39.4,
    },
    "hybrid_regimes": {
        "count": 30, "label": "Hybrid Regimes", "color": "#ca8a04",
        "description": "Score 4.0-5.99: elections irregular; weak rule of law",
        "examples": ["UA", "TH", "BD", "KE", "PK", "TR", "NG", "MA", "HN", "GT"],
        "population_pct": 15.2,
    },
    "authoritarian": {
        "count": 61, "label": "Authoritarian", "color": "#dc2626",
        "description": "Score <4.0: political pluralism absent; repression",
        "examples": ["CN", "RU", "IR", "SA", "EG", "VN", "KP", "AF", "SY", "MM", "BY", "VE", "CU", "SD"],
        "population_pct": 39.7,
    },
}


# ═══════════════ ECONOMIC POWER COMPARISON ═══════════════
ECONOMIC_POWER = {
    "gdp_ranking": [
        {"code": "US", "name": "United States", "gdp_t": 27.4, "gdp_growth": 2.5, "debt_gdp": 123, "trade_balance": -773},
        {"code": "CN", "name": "China", "gdp_t": 18.5, "gdp_growth": 5.0, "debt_gdp": 83, "trade_balance": 823},
        {"code": "DE", "name": "Germany", "gdp_t": 4.5, "gdp_growth": -0.2, "debt_gdp": 64, "trade_balance": 223},
        {"code": "JP", "name": "Japan", "gdp_t": 4.2, "gdp_growth": 1.9, "debt_gdp": 255, "trade_balance": -46},
        {"code": "IN", "name": "India", "gdp_t": 3.9, "gdp_growth": 7.8, "debt_gdp": 81, "trade_balance": -243},
        {"code": "GB", "name": "United Kingdom", "gdp_t": 3.5, "gdp_growth": 0.7, "debt_gdp": 101, "trade_balance": -198},
        {"code": "FR", "name": "France", "gdp_t": 3.1, "gdp_growth": 1.1, "debt_gdp": 111, "trade_balance": -99},
        {"code": "IT", "name": "Italy", "gdp_t": 2.3, "gdp_growth": 0.9, "debt_gdp": 140, "trade_balance": 35},
        {"code": "BR", "name": "Brazil", "gdp_t": 2.2, "gdp_growth": 2.9, "debt_gdp": 87, "trade_balance": 61},
        {"code": "CA", "name": "Canada", "gdp_t": 2.1, "gdp_growth": 1.1, "debt_gdp": 107, "trade_balance": -18},
        {"code": "RU", "name": "Russia", "gdp_t": 2.0, "gdp_growth": 3.6, "debt_gdp": 22, "trade_balance": 140},
        {"code": "KR", "name": "South Korea", "gdp_t": 1.7, "gdp_growth": 1.4, "debt_gdp": 54, "trade_balance": 44},
        {"code": "AU", "name": "Australia", "gdp_t": 1.7, "gdp_growth": 2.0, "debt_gdp": 44, "trade_balance": 65},
        {"code": "MX", "name": "Mexico", "gdp_t": 1.8, "gdp_growth": 3.2, "debt_gdp": 53, "trade_balance": -5},
        {"code": "ES", "name": "Spain", "gdp_t": 1.6, "gdp_growth": 2.5, "debt_gdp": 108, "trade_balance": -38},
    ],
    "share_shift": {
        "title": "Global GDP Share Shift",
        "data": {
            "2000": {"G7": 65, "BRICS": 8, "Rest": 27},
            "2010": {"G7": 51, "BRICS": 18, "Rest": 31},
            "2020": {"G7": 44, "BRICS": 25, "Rest": 31},
            "2025": {"G7": 29, "BRICS": 36, "Rest": 35},
        },
    },
}


# ═══════════════ MILITARY TREND ANALYSIS ═══════════════
# Regions for grouping military assets
_TREND_REGIONS = {
    "Middle East / Gulf": {"lat_min": 12, "lat_max": 42, "lng_min": 30, "lng_max": 65},
    "Eastern Mediterranean": {"lat_min": 30, "lat_max": 42, "lng_min": 18, "lng_max": 36},
    "Western Pacific / Taiwan Strait": {"lat_min": 15, "lat_max": 40, "lng_min": 115, "lng_max": 145},
    "South China Sea": {"lat_min": 0, "lat_max": 22, "lng_min": 100, "lng_max": 120},
    "North Atlantic / GIUK Gap": {"lat_min": 50, "lat_max": 72, "lng_min": -30, "lng_max": 10},
    "Baltic Sea": {"lat_min": 53, "lat_max": 66, "lng_min": 10, "lng_max": 30},
    "Black Sea": {"lat_min": 40, "lat_max": 48, "lng_min": 27, "lng_max": 42},
    "Indian Ocean / Arabian Sea": {"lat_min": -10, "lat_max": 25, "lng_min": 50, "lng_max": 80},
    "Red Sea / Gulf of Aden": {"lat_min": 10, "lat_max": 30, "lng_min": 32, "lng_max": 52},
    "Arctic / Barents Sea": {"lat_min": 66, "lat_max": 85, "lng_min": -30, "lng_max": 60},
    "North Africa / Sahel": {"lat_min": 5, "lat_max": 35, "lng_min": -20, "lng_max": 30},
    "East Africa / Horn": {"lat_min": -5, "lat_max": 15, "lng_min": 30, "lng_max": 55},
    "Korean Peninsula": {"lat_min": 33, "lat_max": 42, "lng_min": 124, "lng_max": 132},
    "Indo-Pacific Central": {"lat_min": -10, "lat_max": 10, "lng_min": 90, "lng_max": 115},
    "Caribbean / Gulf of Mexico": {"lat_min": 15, "lat_max": 32, "lng_min": -100, "lng_max": -60},
}


def _in_region(lat, lng, bounds):
    return bounds["lat_min"] <= lat <= bounds["lat_max"] and bounds["lng_min"] <= lng <= bounds["lng_max"]


def compute_military_trends():
    """Analyze vessel positions, headings, and bases to identify regional trends."""
    trends = []

    # ── 1. Vessel concentration by region ──
    region_vessels = {r: [] for r in _TREND_REGIONS}
    moving_vessels = []

    for v in VESSEL_DEPLOYMENTS:
        for region, bounds in _TREND_REGIONS.items():
            if _in_region(v["lat"], v["lng"], bounds):
                region_vessels[region].append(v)
        if v.get("speed_kts", 0) > 0:
            moving_vessels.append(v)

    # ── 2. Movement vectors — where are assets heading? ──
    heading_to_region = {}  # region → list of vessels heading toward it
    for v in moving_vessels:
        heading = v.get("heading", 0)
        speed = v.get("speed_kts", 0)
        lat, lng = v["lat"], v["lng"]
        # Project position ~24h ahead (rough estimate)
        dist_nm = speed * 24
        dist_deg = dist_nm / 60.0
        proj_lat = lat + dist_deg * math.cos(math.radians(heading))
        proj_lng = lng + dist_deg * math.sin(math.radians(heading))
        proj_lat = max(-90, min(90, proj_lat))
        proj_lng = ((proj_lng + 180) % 360) - 180

        for region, bounds in _TREND_REGIONS.items():
            if _in_region(proj_lat, proj_lng, bounds):
                heading_to_region.setdefault(region, []).append({
                    "vessel": v["name"], "country": v["country"],
                    "from_lat": lat, "from_lng": lng,
                    "proj_lat": round(proj_lat, 2), "proj_lng": round(proj_lng, 2),
                    "heading": heading, "speed_kts": speed,
                })

    # ── 3. Base concentration by region ──
    region_bases = {r: 0 for r in _TREND_REGIONS}
    for b in MILITARY_BASES:
        for region, bounds in _TREND_REGIONS.items():
            if _in_region(b["lat"], b["lng"], bounds):
                region_bases[region] += 1

    # ── 4. Generate trend insights ──
    for region in _TREND_REGIONS:
        vessel_count = len(region_vessels[region])
        base_count = region_bases[region]
        heading_count = len(heading_to_region.get(region, []))
        countries = list(set(v["country"] for v in region_vessels[region]))
        moving_in = heading_to_region.get(region, [])

        # Determine significance
        if vessel_count == 0 and heading_count == 0 and base_count == 0:
            continue

        severity = "low"
        if vessel_count >= 5 or heading_count >= 3:
            severity = "high"
        elif vessel_count >= 3 or heading_count >= 2:
            severity = "medium"

        movement_desc = ""
        if moving_in:
            moving_countries = list(set(m["country"] for m in moving_in))
            movement_desc = f"{heading_count} vessel(s) projected heading toward this region within 24h ({', '.join(moving_countries)})"

        vessel_types = {}
        for v in region_vessels[region]:
            vtype = v.get("type", "unknown").replace("_", " ")
            vessel_types[vtype] = vessel_types.get(vtype, 0) + 1

        trends.append({
            "region": region,
            "severity": severity,
            "vessel_count": vessel_count,
            "base_count": base_count,
            "inbound_count": heading_count,
            "countries_present": countries,
            "vessel_types": vessel_types,
            "movement_trend": movement_desc,
            "vessels": [{"name": v["name"], "country": v["country"], "type": v.get("type", ""),
                         "lat": v["lat"], "lng": v["lng"], "heading": v.get("heading", 0),
                         "speed_kts": v.get("speed_kts", 0)} for v in region_vessels[region]],
            "inbound": moving_in,
        })

    # Sort by severity then vessel count
    sev_order = {"high": 0, "medium": 1, "low": 2}
    trends.sort(key=lambda t: (sev_order.get(t["severity"], 3), -t["vessel_count"]))

    # ── 5. Generate projected positions for all moving vessels (for map arrows) ──
    projections = []
    for v in VESSEL_DEPLOYMENTS:
        speed = v.get("speed_kts", 0)
        if speed <= 0:
            continue
        heading = v.get("heading", 0)
        lat, lng = v["lat"], v["lng"]
        # Project 12h ahead
        dist_nm = speed * 12
        dist_deg = dist_nm / 60.0
        proj_lat = lat + dist_deg * math.cos(math.radians(heading))
        proj_lng = lng + dist_deg * math.sin(math.radians(heading))
        proj_lat = max(-90, min(90, proj_lat))
        proj_lng = ((proj_lng + 180) % 360) - 180
        projections.append({
            "name": v["name"], "country": v["country"],
            "from_lat": lat, "from_lng": lng,
            "to_lat": round(proj_lat, 2), "to_lng": round(proj_lng, 2),
            "heading": heading, "speed_kts": speed,
        })

    return {"trends": trends, "projections": projections, "total_vessels": len(VESSEL_DEPLOYMENTS), "moving_vessels": len(moving_vessels)}


# ── Country Intelligence Index + Correlation Engine ───────────────────────────

def compute_country_intel_index() -> dict:
    """Composite per-country risk index from multiple existing data streams.
    Higher score = higher instability/risk."""
    scores: dict = {}

    def _bump(code: str, weight: float, driver: str):
        if not code:
            return
        entry = scores.setdefault(code, {"score": 0.0, "drivers": []})
        entry["score"] += weight
        if driver and driver not in entry["drivers"]:
            entry["drivers"].append(driver)

    for code, idx in COUNTRY_INDICES.items():
        s = scores.setdefault(code, {"score": 0.0, "drivers": []})
        fs = idx.get("fragile_state", 50)
        s["score"] += min(30.0, fs * 0.25)
        di = idx.get("democracy_index", 5.0)
        s["score"] += (10 - di) * 1.0
        cpi = idx.get("corruption_cpi", 50)
        s["score"] += (100 - cpi) * 0.10
        pf = idx.get("press_freedom", 30)
        s["score"] += pf * 0.08

    for h in HOTSPOTS:
        intensity = h.get("intensity", 3)
        _bump(h.get("country", ""), intensity * 2.5, f"Hotspot: {h.get('name', '')}")
    for m in MILITIAS:
        _bump(m.get("country", ""), 2.0, f"Armed group: {m.get('name', '')}")
    for d in DISEASE_OUTBREAKS:
        sev = d.get("severity", "MEDIUM")
        weight = {"LOW": 1.0, "MEDIUM": 2.0, "HIGH": 4.0, "EXTREME": 6.0}.get(sev, 2.0)
        _bump(d.get("country", ""), weight, f"Outbreak: {d.get('disease', '')}")
    for p in PROTEST_EVENTS:
        _bump(p.get("country", ""), 2.5, f"Protest: {p.get('name', '')}")
    for o in INTERNET_OUTAGES:
        sev = o.get("severity", "MEDIUM")
        weight = {"LOW": 1.0, "MEDIUM": 2.0, "HIGH": 3.5, "EXTREME": 5.0}.get(sev, 2.0)
        _bump(o.get("country", ""), weight, f"{o.get('type', 'outage').upper()}: {o.get('name', '')}")
    for s_entry in SANCTIONS:
        code = s_entry.get("country") or s_entry.get("target") or ""
        _bump(code, 3.0, f"Sanctioned: {s_entry.get('name', s_entry.get('target', ''))}")
    for c in CYBER_ADVISORIES:
        sev = c.get("severity", "MEDIUM")
        weight = {"MEDIUM": 1.5, "HIGH": 3.0, "CRITICAL": 5.0}.get(sev, 1.5)
        _bump(c.get("country", ""), weight, f"Cyber: {c.get('id', '')}")
    for g in GPS_JAMMING_ZONES:
        code = (g.get("country") or "").split("/")[0]
        intensity = g.get("intensity", "MEDIUM")
        weight = {"MEDIUM": 1.0, "HIGH": 2.5, "EXTREME": 4.0}.get(intensity, 1.0)
        _bump(code, weight, f"GPS jamming: {g.get('name', '')}")
    for f in DISPLACEMENT_FLOWS:
        pop = f.get("population", 0)
        weight = min(8.0, math.log10(max(1, pop)) * 1.5)
        _bump(f.get("from_country", ""), weight, f"Refugee origin ({pop:,})")
        _bump(f.get("to_country", ""), weight * 0.4, f"Refugee host ({pop:,})")
    for a in AIR_QUALITY:
        pm = a.get("pm25", 0)
        if pm >= 50:
            _bump(a.get("country", ""), min(6.0, (pm - 50) * 0.05), f"PM2.5 {pm} µg/m³ in {a.get('name', '')}")
    for d in SUPPLY_CHAIN_DISRUPTIONS:
        sev = d.get("severity", "MEDIUM")
        weight = {"MEDIUM": 1.5, "HIGH": 3.0, "EXTREME": 5.0}.get(sev, 1.5)
        _bump(d.get("country", ""), weight, f"Supply chain: {d.get('name', '')}")

    for code in scores:
        scores[code]["score"] = round(min(100.0, scores[code]["score"]), 1)
        scores[code]["drivers"] = scores[code]["drivers"][:8]

    ranked = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)
    for i, (code, _data) in enumerate(ranked):
        scores[code]["rank"] = i + 1
    return scores


def compute_correlation_signals() -> list:
    """Cross-stream correlation engine: detect countries with multiple convergent signals."""
    by_country: dict = {}

    def _add(code: str, signal_type: str, label: str):
        if not code:
            return
        entry = by_country.setdefault(code, {"signals": {}, "labels": []})
        entry["signals"][signal_type] = entry["signals"].get(signal_type, 0) + 1
        entry["labels"].append(f"{signal_type}: {label}")

    for h in HOTSPOTS:
        _add(h.get("country", ""), "armed_conflict", h.get("name", ""))
    for m in MILITIAS:
        _add(m.get("country", ""), "armed_group", m.get("name", ""))
    for d in DISEASE_OUTBREAKS:
        _add(d.get("country", ""), "outbreak", d.get("disease", ""))
    for p in PROTEST_EVENTS:
        _add(p.get("country", ""), "civil_unrest", p.get("name", ""))
    for o in INTERNET_OUTAGES:
        _add(o.get("country", ""), "cyber_outage", o.get("name", ""))
    for s_entry in SANCTIONS:
        _add(s_entry.get("country", "") or s_entry.get("target", ""), "sanctions", s_entry.get("name", s_entry.get("target", "")))
    for c in CYBER_ADVISORIES:
        _add(c.get("country", ""), "cyber_threat", c.get("title", ""))
    for f in DISPLACEMENT_FLOWS:
        _add(f.get("from_country", ""), "displacement", f.get("name", ""))
    for d in SUPPLY_CHAIN_DISRUPTIONS:
        _add(d.get("country", ""), "supply_chain", d.get("name", ""))
    for a in AIR_QUALITY:
        if a.get("pm25", 0) >= 100:
            _add(a.get("country", ""), "air_pollution", f"{a.get('name', '')} PM2.5 {a.get('pm25', 0)}")
    for g in GPS_JAMMING_ZONES:
        c0 = (g.get("country") or "").split("/")[0]
        _add(c0, "gps_jamming", g.get("name", ""))

    alerts = []
    for code, info in by_country.items():
        signals = info["signals"]
        signal_count = len(signals)
        total = sum(signals.values())
        if signal_count >= 3:
            severity = "EXTREME" if signal_count >= 6 else ("HIGH" if signal_count >= 5 else "MEDIUM")
            alerts.append({
                "country": code,
                "signal_types": signal_count,
                "total_signals": total,
                "severity": severity,
                "signals": signals,
                "labels": info["labels"][:10],
            })
    alerts.sort(key=lambda x: (x["signal_types"], x["total_signals"]), reverse=True)
    return alerts


# ── API Routes ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    if not HTML_PATH.exists():
        return HTMLResponse("<h1>index.html not found</h1>", status_code=500)
    return HTMLResponse(HTML_PATH.read_text())


def _json(data):
    return Response(content=json.dumps(data, ensure_ascii=True), media_type="application/json")


# ── FX rates proxy (frankfurter.dev) ────────────────────────────────────
_FX_CACHE: dict = {"data": None, "fetched_at": 0.0}
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


def _fetch_fx_blocking() -> dict | None:
    """Fetch latest USD-base FX rates from frankfurter.dev (blocking)."""
    try:
        req = urllib.request.Request(
            "https://api.frankfurter.dev/v1/latest?base=USD",
            headers={"User-Agent": "narve-world-state/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                data.setdefault("rates", {})
                data["rates"]["USD"] = 1.0
                return data
    except Exception as e:
        logging.warning("FX rate fetch failed: %s", e)
    return None


@app.get("/api/fx-rates")
async def get_fx_rates():
    """USD-base FX rates, cached for 1h. Source: frankfurter.dev."""
    now = time.time()
    cached = _FX_CACHE["data"]
    if cached and (now - _FX_CACHE["fetched_at"]) < _FX_TTL:
        return cached
    data = await asyncio.to_thread(_fetch_fx_blocking)
    if data:
        _FX_CACHE["data"] = data
        _FX_CACHE["fetched_at"] = now
        return data
    if cached:
        return cached
    return _FX_FALLBACK


@app.get("/api/conflicts")
async def get_conflicts():
    return _json({"conflicts": CONFLICTS, "hotspots": HOTSPOTS})


@app.get("/api/indicators")
async def get_indicators():
    return _json({"indicators": INDICATORS})


@app.get("/api/geopolitical")
async def get_geopolitical():
    return _json({
        "sanctions": SANCTIONS,
        "nuclear": NUCLEAR_ARSENALS,
        "threat_level": "ELEVATED",
        "threat_note": "Multiple active high-intensity conflicts. Elevated nuclear rhetoric. Global trade disruptions.",
    })


@app.get("/api/news")
async def get_news():
    news = await asyncio.to_thread(fetch_news)
    return _json({"news": news})


@app.get("/api/polymarket")
async def get_polymarket():
    markets = await asyncio.to_thread(fetch_polymarket)
    return _json({"markets": markets})


@app.get("/api/xfeed")
async def get_xfeed():
    posts = await asyncio.to_thread(fetch_xfeed)
    return _json({
        "posts": posts,
        "count": len(posts),
        "accounts_tracked": len(X_ACCOUNTS),
        "has_token": bool(X_BEARER_TOKEN),
    })


@app.get("/api/militias")
async def get_militias():
    return _json({"militias": MILITIAS})


@app.get("/api/relations/{country_code}")
async def get_relations(country_code: str):
    code = country_code.upper()
    if code in COUNTRY_RELATIONS:
        return _json(COUNTRY_RELATIONS[code])
    return Response(content=json.dumps({"error": "Country not found"}), status_code=404, media_type="application/json")


@app.get("/api/relations")
async def get_all_relations():
    return _json({"countries": list(COUNTRY_RELATIONS.keys()), "data": COUNTRY_RELATIONS})


@app.get("/api/country/{code}")
async def get_country(code: str):
    c = code.upper()
    if c in COUNTRY_PROFILES:
        return _json(COUNTRY_PROFILES[c])
    return Response(content=json.dumps({"error": "Country profile not found"}), status_code=404, media_type="application/json")


@app.get("/api/profiles")
async def get_profiles():
    return _json({"countries": list(COUNTRY_PROFILES.keys()), "data": COUNTRY_PROFILES})


@app.get("/api/trends")
async def get_trends():
    return _json({
        "trends": GLOBAL_TRENDS,
        "democracy": DEMOCRACY_TRACKER,
        "blocs": ALLIANCE_BLOCS,
        "economic": ECONOMIC_POWER,
        "indices": COUNTRY_INDICES,
    })


@app.get("/api/military-trends")
async def get_military_trends():
    return _json(compute_military_trends())


@app.get("/api/aircraft")
async def get_aircraft():
    all_planes = await asyncio.to_thread(fetch_opensky)
    mil = filter_military_aircraft(all_planes)
    return _json({"aircraft": mil, "count": len(mil), "total": len(all_planes)})


@app.get("/api/facilities")
async def get_facilities():
    return _json({"facilities": NUCLEAR_FACILITIES, "bases": MILITARY_BASES, "vessels": VESSEL_DEPLOYMENTS, "spaceports": SPACEPORTS, "air_forces": WORLD_AIR_FORCES, "armies": WORLD_ARMIES})


@app.get("/api/disasters")
async def get_disasters():
    events = await asyncio.to_thread(fetch_eonet)
    return _json({"disasters": events, "count": len(events)})


@app.get("/api/satellites")
async def get_satellites():
    sats = await asyncio.to_thread(fetch_satellites)
    return _json({"satellites": sats, "count": len(sats)})


@app.get("/api/earthquakes")
async def get_earthquakes():
    quakes = await asyncio.to_thread(fetch_earthquakes)
    return _json({"earthquakes": quakes, "count": len(quakes)})


@app.get("/api/wildfires")
async def get_wildfires():
    fires = await asyncio.to_thread(fetch_wildfires)
    return _json({"wildfires": fires, "count": len(fires)})


@app.get("/api/world-state")
async def get_world_state():
    return _json({
        "chokepoints": STRATEGIC_CHOKEPOINTS,
        "stock_exchanges": STOCK_EXCHANGES,
        "central_banks": CENTRAL_BANKS,
        "mining_sites": MINING_SITES,
        "diseases": DISEASE_OUTBREAKS,
        "protests": PROTEST_EVENTS,
        "internet_outages": INTERNET_OUTAGES,
    })


@app.get("/api/infrastructure")
async def get_infrastructure():
    return _json({
        "capitals": CAPITAL_CITIES,
        "data_centers": MAJOR_DATA_CENTERS,
        "undersea_cables": UNDERSEA_CABLES,
        "pipelines": OIL_GAS_PIPELINES,
        "reactors": NUCLEAR_REACTORS,
        "shipping_routes": SHIPPING_ROUTES,
        "industrial_centers": INDUSTRIAL_CENTERS,
        "economic_zones": ECONOMIC_ZONES,
        "oil_rare_earth": OIL_RARE_EARTH_FIELDS,
    })


@app.get("/api/all")
async def get_all():
    # Fetch live data in parallel (including cross-dashboard)
    quakes_task = asyncio.to_thread(fetch_earthquakes)
    fires_task = asyncio.to_thread(fetch_wildfires)
    cross_task = cross_dashboard.fetch_all()
    quakes, fires, cross_data = await asyncio.gather(
        quakes_task, fires_task, cross_task, return_exceptions=True
    )
    if isinstance(quakes, Exception):
        quakes = []
    if isinstance(fires, Exception):
        fires = []
    if isinstance(cross_data, Exception):
        cross_data = {}
    return _json({
        # ── Cross-dashboard live data ──
        "midterm_elections": cross_data.get("midterm_elections", {}),
        "crypto_signals": cross_data.get("crypto_signals", {}),
        "_cross_meta": cross_data.get("_cross_meta", {}),
        "conflicts": CONFLICTS,
        "hotspots": HOTSPOTS,
        "indicators": INDICATORS,
        "sanctions": SANCTIONS,
        "nuclear": NUCLEAR_ARSENALS,
        "militias": MILITIAS,
        "relations": COUNTRY_RELATIONS,
        "profiles": COUNTRY_PROFILES,
        "trends": GLOBAL_TRENDS,
        "democracy": DEMOCRACY_TRACKER,
        "blocs": ALLIANCE_BLOCS,
        "economic": ECONOMIC_POWER,
        "indices": COUNTRY_INDICES,
        "facilities": NUCLEAR_FACILITIES,
        "bases": MILITARY_BASES,
        "vessels": VESSEL_DEPLOYMENTS,
        "capitals": CAPITAL_CITIES,
        "data_centers": MAJOR_DATA_CENTERS,
        "undersea_cables": UNDERSEA_CABLES,
        "pipelines": OIL_GAS_PIPELINES,
        "reactors": NUCLEAR_REACTORS,
        "shipping_routes": SHIPPING_ROUTES,
        "industrial_centers": INDUSTRIAL_CENTERS,
        "economic_zones": ECONOMIC_ZONES,
        "oil_rare_earth": OIL_RARE_EARTH_FIELDS,
        "spaceports": SPACEPORTS,
        "air_forces": WORLD_AIR_FORCES,
        "armies": WORLD_ARMIES,
        "earthquakes": quakes,
        "wildfires": fires,
        "chokepoints": STRATEGIC_CHOKEPOINTS,
        "stock_exchanges": STOCK_EXCHANGES,
        "central_banks": CENTRAL_BANKS,
        "mining_sites": MINING_SITES,
        "diseases": DISEASE_OUTBREAKS,
        "protests": PROTEST_EVENTS,
        "internet_outages": INTERNET_OUTAGES,
        "cyber_advisories": CYBER_ADVISORIES,
        "gps_jamming": GPS_JAMMING_ZONES,
        "displacement_flows": DISPLACEMENT_FLOWS,
        "air_quality": AIR_QUALITY,
        "supply_chain": SUPPLY_CHAIN_DISRUPTIONS,
        "sea_ice_arctic": ARCTIC_SEA_ICE,
        "sea_ice_antarctic": ANTARCTIC_SEA_ICE,
        "volcanoes": ACTIVE_VOLCANOES,
        "ai_datacenters": AI_DATA_CENTERS,
        "tech_hqs": TECH_HQS,
        "startup_hubs": STARTUP_HUBS,
        "financial_centers": FINANCIAL_CENTERS,
        "commodity_hubs": COMMODITY_HUBS,
        "trade_routes": TRADE_ROUTES,
        "intel_hotspots": INTEL_HOTSPOTS,
        "sanctions_pressure": SANCTIONS_PRESSURE,
        "live_webcams": LIVE_WEBCAMS,
        "country_intel_index": compute_country_intel_index(),
        "correlation_alerts": compute_correlation_signals(),
        "apt_groups": APT_GROUPS,
        "firms_fires": NASA_FIRMS_FIRES,
        "aviation_airports": AVIATION_AIRPORTS,
        "notam_closures": NOTAM_CLOSURES,
        "climate_anomalies": CLIMATE_ANOMALIES,
        "wto_restrictions": WTO_TRADE_RESTRICTIONS,
        "bis_rates": BIS_POLICY_RATES,
        "sector_heatmap": SECTOR_HEATMAP,
        "oil_analytics": OIL_ANALYTICS,
        "world_energy": {
            "mix": WORLD_ENERGY_MIX,
            "regions": WORLD_ENERGY_REGIONS,
            "top_producers": WORLD_ENERGY_TOP_PRODUCERS,
            "chokepoints": ENERGY_CHOKEPOINTS,
            "lng_hubs": ENERGY_LNG_HUBS,
            "forecast": ENERGY_FORECAST,
            "derivatives": ENERGY_DERIVATIVES,
        },
        "btc_etfs": BTC_ETF_FLOWS,
        "stablecoins": STABLECOINS,
        "gov_spending": GOV_SPENDING,
        "layoffs": LAYOFFS_TRACKER,
        "israel_sirens": ISRAEL_SIRENS,
        "telegram_intel": TELEGRAM_INTEL,
        "tech_readiness": TECH_READINESS,
        "strategic_posture": STRATEGIC_POSTURE,
        "live_intel_feeds": LIVE_INTELLIGENCE_FEEDS,
        "population_exposure": POPULATION_EXPOSURE,
        "strategic_risk": _compute_strategic_risk(),
        "market_indices": MARKET_INDICES,
        "fear_greed": FEAR_GREED_INDEX,
        "yield_curve_us": YIELD_CURVE_US,
        "global_bond_yields": GLOBAL_BOND_YIELDS,
        "commodity_prices": COMMODITY_PRICES,
        "etf_flows": ETF_FLOWS,
        "earnings_calendar": EARNINGS_CALENDAR,
        "cot_report": COT_REPORT,
        "gdelt_events": GDELT_EVENTS,
        "global_conflict_index": GLOBAL_CONFLICT_INDEX,
        "humanitarian_crises": HUMANITARIAN_CRISES,
        "world_clock_zones": WORLD_CLOCK_ZONES,
        "national_debt": NATIONAL_DEBT,
        "threat_level": "ELEVATED",
        "threat_note": "Multiple active high-intensity conflicts. Elevated nuclear rhetoric. Global trade disruptions.",
        "xfeed_accounts": len(X_ACCOUNTS),
        "xfeed_has_token": bool(X_BEARER_TOKEN),
    })


# ── New endpoints (worldmonitor-style features) ──────────────────────────────

@app.get("/api/cyber-advisories")
async def get_cyber_advisories():
    return _json({"advisories": CYBER_ADVISORIES, "count": len(CYBER_ADVISORIES)})


@app.get("/api/gps-jamming")
async def get_gps_jamming():
    return _json({"zones": GPS_JAMMING_ZONES, "count": len(GPS_JAMMING_ZONES)})


@app.get("/api/displacement")
async def get_displacement():
    total = sum(f.get("population", 0) for f in DISPLACEMENT_FLOWS)
    return _json({"flows": DISPLACEMENT_FLOWS, "count": len(DISPLACEMENT_FLOWS), "total_displaced": total})


@app.get("/api/air-quality")
async def get_air_quality():
    return _json({"readings": AIR_QUALITY, "count": len(AIR_QUALITY)})


@app.get("/api/sea-ice")
async def get_sea_ice():
    return _json({"arctic": ARCTIC_SEA_ICE, "antarctic": ANTARCTIC_SEA_ICE})


@app.get("/api/supply-chain")
async def get_supply_chain():
    return _json({"disruptions": SUPPLY_CHAIN_DISRUPTIONS, "count": len(SUPPLY_CHAIN_DISRUPTIONS)})


@app.get("/api/volcanoes")
async def get_volcanoes():
    erupting = [v for v in ACTIVE_VOLCANOES if v.get("status") == "ERUPTING"]
    return _json({"volcanoes": ACTIVE_VOLCANOES, "count": len(ACTIVE_VOLCANOES), "erupting": len(erupting)})


@app.get("/api/ai-datacenters")
async def get_ai_datacenters():
    total_chips = sum(d.get("chip_count", 0) for d in AI_DATA_CENTERS)
    total_mw = sum(d.get("power_mw", 0) for d in AI_DATA_CENTERS)
    return _json({"datacenters": AI_DATA_CENTERS, "count": len(AI_DATA_CENTERS), "total_chips": total_chips, "total_mw": total_mw})


@app.get("/api/tech-hqs")
async def get_tech_hqs():
    total_mcap = sum(h.get("mcap", 0) for h in TECH_HQS)
    return _json({"hqs": TECH_HQS, "count": len(TECH_HQS), "total_mcap_b": total_mcap})


@app.get("/api/startup-hubs")
async def get_startup_hubs():
    return _json({"hubs": STARTUP_HUBS, "count": len(STARTUP_HUBS)})


@app.get("/api/financial-centers")
async def get_financial_centers():
    return _json({"centers": FINANCIAL_CENTERS, "count": len(FINANCIAL_CENTERS)})


@app.get("/api/commodity-hubs")
async def get_commodity_hubs():
    return _json({"hubs": COMMODITY_HUBS, "count": len(COMMODITY_HUBS)})


@app.get("/api/trade-routes")
async def get_trade_routes():
    return _json({"routes": TRADE_ROUTES, "count": len(TRADE_ROUTES)})


@app.get("/api/intel-hotspots")
async def get_intel_hotspots():
    return _json({"hotspots": INTEL_HOTSPOTS, "count": len(INTEL_HOTSPOTS)})


@app.get("/api/sanctions-pressure")
async def get_sanctions_pressure():
    return _json({"countries": SANCTIONS_PRESSURE, "count": len(SANCTIONS_PRESSURE)})


@app.get("/api/webcams")
async def get_webcams():
    return _json({"webcams": LIVE_WEBCAMS, "count": len(LIVE_WEBCAMS)})


@app.get("/api/intel-index")
async def get_intel_index():
    idx = compute_country_intel_index()
    top10 = sorted(
        [{"country": c, **info} for c, info in idx.items()],
        key=lambda x: x["score"],
        reverse=True,
    )[:10]
    return _json({"index": idx, "top_10": top10, "country_count": len(idx)})


@app.get("/api/correlations")
async def get_correlations():
    alerts = compute_correlation_signals()
    return _json({"alerts": alerts, "count": len(alerts)})


@app.get("/api/sanctions/search")
async def search_sanctions(q: str = ""):
    """OFAC-style entity search across our SANCTIONS list."""
    q_low = (q or "").strip().lower()
    if not q_low:
        return _json({"query": "", "results": [], "count": 0})
    out = []
    for s in SANCTIONS:
        hay = " ".join([
            str(s.get("name", "")),
            str(s.get("target", "")),
            str(s.get("country", "")),
            str(s.get("type", "")),
            str(s.get("detail", "")),
        ]).lower()
        if q_low in hay:
            out.append(s)
    return _json({"query": q, "results": out, "count": len(out)})


# ── Phase 3: extended worldmonitor-parity endpoints ──────────────────────────


@app.get("/api/apt-groups")
async def get_apt_groups():
    return _json({"groups": APT_GROUPS, "count": len(APT_GROUPS)})


@app.get("/api/firms-fires")
async def get_firms_fires():
    return _json({"fires": NASA_FIRMS_FIRES, "count": len(NASA_FIRMS_FIRES)})


@app.get("/api/aviation")
async def get_aviation():
    closed = [a for a in AVIATION_AIRPORTS if a.get("ground_stop")]
    severe = [a for a in AVIATION_AIRPORTS if a.get("delay_status") in ("SEVERE", "MAJOR")]
    return _json({
        "airports": AVIATION_AIRPORTS,
        "notams": NOTAM_CLOSURES,
        "ground_stops": len(closed),
        "severe_delays": len(severe),
        "count": len(AVIATION_AIRPORTS),
    })


@app.get("/api/climate-anomalies")
async def get_climate_anomalies():
    return _json({"anomalies": CLIMATE_ANOMALIES, "count": len(CLIMATE_ANOMALIES)})


@app.get("/api/wto-restrictions")
async def get_wto_restrictions():
    active = [w for w in WTO_TRADE_RESTRICTIONS if w.get("status") == "ACTIVE"]
    return _json({"restrictions": WTO_TRADE_RESTRICTIONS, "active": len(active), "count": len(WTO_TRADE_RESTRICTIONS)})


@app.get("/api/bis-rates")
async def get_bis_rates():
    avg = sum(b["rate_pct"] for b in BIS_POLICY_RATES) / max(1, len(BIS_POLICY_RATES))
    return _json({"banks": BIS_POLICY_RATES, "global_avg_rate_pct": round(avg, 2), "count": len(BIS_POLICY_RATES)})


@app.get("/api/sector-heatmap")
async def get_sector_heatmap():
    return _json({"sectors": SECTOR_HEATMAP, "count": len(SECTOR_HEATMAP)})


@app.get("/api/oil-analytics")
async def get_oil_analytics():
    return _json(OIL_ANALYTICS)


@app.get("/api/world-energy")
async def get_world_energy():
    """Energy panel payload — production mix, chokepoints, forecast, derivatives.

    Static snapshot data sourced from OWID (Ember + Energy Institute) for the
    mix/regions/forecast, EIA chokepoint reports + GIIGNL for transit, and
    Stooq for derivatives. Refresh by editing the dataclasses inline."""
    return _json({
        "mix":           WORLD_ENERGY_MIX,
        "regions":       WORLD_ENERGY_REGIONS,
        "top_producers": WORLD_ENERGY_TOP_PRODUCERS,
        "chokepoints":   ENERGY_CHOKEPOINTS,
        "lng_hubs":      ENERGY_LNG_HUBS,
        "forecast":      ENERGY_FORECAST,
        "derivatives":   ENERGY_DERIVATIVES,
    })


@app.get("/api/btc-etfs")
async def get_btc_etfs():
    total_aum = sum(e["aum_b"] for e in BTC_ETF_FLOWS)
    flow_24h = sum(e["flow_24h_m"] for e in BTC_ETF_FLOWS)
    return _json({"etfs": BTC_ETF_FLOWS, "total_aum_b": round(total_aum, 2), "net_flow_24h_m": round(flow_24h, 2), "count": len(BTC_ETF_FLOWS)})


@app.get("/api/stablecoins")
async def get_stablecoins():
    total = sum(s["marketcap_b"] for s in STABLECOINS)
    return _json({"stablecoins": STABLECOINS, "total_marketcap_b": round(total, 2), "count": len(STABLECOINS)})


@app.get("/api/gov-spending")
async def get_gov_spending():
    total = sum(g["amount_m"] for g in GOV_SPENDING)
    return _json({"contracts": GOV_SPENDING, "total_m": total, "count": len(GOV_SPENDING)})


@app.get("/api/layoffs")
async def get_layoffs():
    total = sum(l["count"] for l in LAYOFFS_TRACKER)
    return _json({"layoffs": LAYOFFS_TRACKER, "total_jobs": total, "count": len(LAYOFFS_TRACKER)})


@app.get("/api/israel-sirens")
async def get_israel_sirens():
    return _json({"sirens": ISRAEL_SIRENS, "count": len(ISRAEL_SIRENS)})


@app.get("/api/telegram-intel")
async def get_telegram_intel():
    return _json({"channels": TELEGRAM_INTEL, "count": len(TELEGRAM_INTEL)})


@app.get("/api/tech-readiness")
async def get_tech_readiness():
    return _json({"tech": TECH_READINESS, "count": len(TECH_READINESS)})


@app.get("/api/strategic-posture")
async def get_strategic_posture():
    return _json({"theaters": STRATEGIC_POSTURE, "count": len(STRATEGIC_POSTURE)})


@app.get("/api/live-intel")
async def get_live_intel(topic: str = ""):
    t = (topic or "").strip().lower()
    if t and t in LIVE_INTELLIGENCE_FEEDS:
        return _json({"topic": t, "items": LIVE_INTELLIGENCE_FEEDS[t]})
    return _json({"feeds": LIVE_INTELLIGENCE_FEEDS, "topics": list(LIVE_INTELLIGENCE_FEEDS.keys())})


@app.get("/api/population-exposure")
async def get_population_exposure():
    return _json({"regions": POPULATION_EXPOSURE, "count": len(POPULATION_EXPOSURE)})


def _compute_strategic_risk():
    """Composite risk score across modules. Used by /api/all and /api/strategic-risk."""
    try:
        intel = compute_country_intel_index()
        avg_score = sum(c["score"] for c in intel.values()) / max(1, len(intel))
        high_intel_countries = sum(1 for c in intel.values() if c["score"] >= 50)
    except Exception:
        avg_score = 0.0
        high_intel_countries = 0
    erupting_volcanoes = sum(1 for v in ACTIVE_VOLCANOES if v.get("status") == "ERUPTING")
    extreme_climate = sum(1 for c in CLIMATE_ANOMALIES if (c.get("severity") or "").upper() == "EXTREME")
    extreme_supply = sum(1 for s in SUPPLY_CHAIN_DISRUPTIONS if (s.get("severity") or "").upper() == "EXTREME")
    critical_cyber = sum(1 for c in CYBER_ADVISORIES if (c.get("severity") or "").upper() == "CRITICAL")
    active_conflicts = len(CONFLICTS)
    grounded = sum(1 for a in AVIATION_AIRPORTS if a.get("ground_stop"))
    composite = min(100, round(
        avg_score * 0.30
        + high_intel_countries * 1.5
        + erupting_volcanoes * 0.6
        + extreme_climate * 1.8
        + extreme_supply * 2.4
        + critical_cyber * 1.6
        + active_conflicts * 1.2
        + grounded * 1.0,
        1,
    ))
    if composite >= 75:
        level = "CRITICAL"
    elif composite >= 60:
        level = "HIGH"
    elif composite >= 40:
        level = "ELEVATED"
    elif composite >= 20:
        level = "MODERATE"
    else:
        level = "LOW"
    return {
        "composite": composite,
        "level": level,
        "modules": {
            "country_intel": round(avg_score, 1),
            "high_intel": high_intel_countries,
            "conflicts": active_conflicts,
            "supply": extreme_supply,
            "climate": extreme_climate,
            "volcanoes": erupting_volcanoes,
            "cyber": critical_cyber,
            "grounded": grounded,
        },
    }


@app.get("/api/strategic-risk")
async def get_strategic_risk():
    return _json(_compute_strategic_risk())


@app.get("/api/country-brief")
async def get_country_brief(code: str = ""):
    """Generate a country brief: profile + indicators + intel score + recent events."""
    code = (code or "").upper().strip()
    if not code:
        return _json({"error": "code parameter required"})
    profile = COUNTRY_PROFILES.get(code) if "COUNTRY_PROFILES" in globals() else None
    indices = COUNTRY_INDICES.get(code) if "COUNTRY_INDICES" in globals() else None
    intel = compute_country_intel_index().get(code)
    cyber = [c for c in CYBER_ADVISORIES if c.get("country") == code]
    sanctions = [s for s in SANCTIONS if s.get("country") == code or s.get("target") == code]
    militias = [m for m in MILITIAS if m.get("country") == code]
    outbreaks = [d for d in DISEASE_OUTBREAKS if d.get("country") == code]
    apts = [a for a in APT_GROUPS if a.get("sponsor") == code]
    layoffs = [l for l in LAYOFFS_TRACKER if l.get("country") == code]
    return _json({
        "code": code,
        "profile": profile,
        "indices": indices,
        "intel_index": intel,
        "cyber_advisories": cyber,
        "sanctions": sanctions[:10],
        "militias": militias,
        "outbreaks": outbreaks,
        "apt_groups": apts,
        "recent_layoffs": layoffs,
    })


# ── Tier 2 markets / intel / utility endpoints ────────────────────────────────

@app.get("/api/market-indices")
async def get_market_indices():
    by_region: dict[str, list] = {}
    for idx in MARKET_INDICES:
        by_region.setdefault(idx.get("region", "Other"), []).append(idx)
    advancers = sum(1 for idx in MARKET_INDICES if idx.get("ch_pct", 0) > 0)
    decliners = sum(1 for idx in MARKET_INDICES if idx.get("ch_pct", 0) < 0)
    return _json({
        "indices": MARKET_INDICES,
        "by_region": by_region,
        "advancers": advancers,
        "decliners": decliners,
        "count": len(MARKET_INDICES),
    })


@app.get("/api/fear-greed")
async def get_fear_greed():
    return _json(FEAR_GREED_INDEX)


@app.get("/api/yield-curve")
async def get_yield_curve():
    twos = next((y["yield"] for y in YIELD_CURVE_US if y["tenor"] == "2Y"), None)
    tens = next((y["yield"] for y in YIELD_CURVE_US if y["tenor"] == "10Y"), None)
    spread_10y_2y = round(tens - twos, 2) if tens is not None and twos is not None else None
    inverted = spread_10y_2y is not None and spread_10y_2y < 0
    return _json({
        "us_curve": YIELD_CURVE_US,
        "global_10y": GLOBAL_BOND_YIELDS,
        "spread_10y_2y": spread_10y_2y,
        "inverted": inverted,
    })


@app.get("/api/commodities")
async def get_commodities():
    by_cat: dict[str, list] = {}
    for c in COMMODITY_PRICES:
        by_cat.setdefault(c.get("category", "other"), []).append(c)
    return _json({
        "commodities": COMMODITY_PRICES,
        "by_category": by_cat,
        "count": len(COMMODITY_PRICES),
    })


@app.get("/api/etf-flows")
async def get_etf_flows():
    by_cat: dict[str, list] = {}
    for e in ETF_FLOWS:
        by_cat.setdefault(e.get("category", "other"), []).append(e)
    total_aum = sum(e.get("aum_b", 0) for e in ETF_FLOWS)
    total_5d_flow = sum(e.get("flow_5d_b", 0) for e in ETF_FLOWS)
    return _json({
        "etfs": ETF_FLOWS,
        "by_category": by_cat,
        "total_aum_b": round(total_aum, 1),
        "total_5d_flow_b": round(total_5d_flow, 2),
        "count": len(ETF_FLOWS),
    })


@app.get("/api/earnings-calendar")
async def get_earnings_calendar():
    high = [e for e in EARNINGS_CALENDAR if e.get("importance") == "high"]
    return _json({
        "calendar": EARNINGS_CALENDAR,
        "high_importance_count": len(high),
        "count": len(EARNINGS_CALENDAR),
    })


@app.get("/api/cot-report")
async def get_cot_report():
    by_cat: dict[str, list] = {}
    for c in COT_REPORT:
        by_cat.setdefault(c.get("category", "other"), []).append(c)
    return _json({
        "contracts": COT_REPORT,
        "by_category": by_cat,
        "count": len(COT_REPORT),
    })


@app.get("/api/gdelt-events")
async def get_gdelt_events():
    avg_tone = round(sum(e.get("tone", 0) for e in GDELT_EVENTS) / max(1, len(GDELT_EVENTS)), 2)
    avg_goldstein = round(sum(e.get("goldstein", 0) for e in GDELT_EVENTS) / max(1, len(GDELT_EVENTS)), 2)
    return _json({
        "events": GDELT_EVENTS,
        "count": len(GDELT_EVENTS),
        "avg_tone": avg_tone,
        "avg_goldstein": avg_goldstein,
    })


@app.get("/api/global-conflict-index")
async def get_global_conflict_index():
    high = [c for c in GLOBAL_CONFLICT_INDEX if c.get("score", 0) >= 80]
    total_fatalities = sum(c.get("fatalities_30d", 0) for c in GLOBAL_CONFLICT_INDEX)
    return _json({
        "countries": GLOBAL_CONFLICT_INDEX,
        "high_conflict_count": len(high),
        "total_fatalities_30d": total_fatalities,
        "count": len(GLOBAL_CONFLICT_INDEX),
    })


@app.get("/api/humanitarian-crises")
async def get_humanitarian_crises():
    total_in_need = sum(h.get("people_in_need_m", 0) for h in HUMANITARIAN_CRISES)
    total_displaced = sum(h.get("displaced_m", 0) for h in HUMANITARIAN_CRISES)
    total_funding_required = sum(h.get("funding_required_b", 0) for h in HUMANITARIAN_CRISES)
    avg_funding_pct = round(sum(h.get("funding_pct", 0) for h in HUMANITARIAN_CRISES) / max(1, len(HUMANITARIAN_CRISES)), 1)
    return _json({
        "crises": HUMANITARIAN_CRISES,
        "total_in_need_m": round(total_in_need, 1),
        "total_displaced_m": round(total_displaced, 1),
        "total_funding_required_b": round(total_funding_required, 1),
        "avg_funding_pct": avg_funding_pct,
        "count": len(HUMANITARIAN_CRISES),
    })


@app.get("/api/world-clock")
async def get_world_clock():
    from datetime import datetime, timezone, timedelta
    now_utc = datetime.now(timezone.utc)
    zones = []
    for z in WORLD_CLOCK_ZONES:
        offset_hours = z.get("utc_offset", 0)
        local = now_utc + timedelta(hours=offset_hours)
        zones.append({
            **z,
            "local_iso": local.strftime("%Y-%m-%dT%H:%M:%S"),
            "local_hour": local.hour,
            "local_minute": local.minute,
            "is_business_hours": 9 <= local.hour < 17 and local.weekday() < 5,
        })
    return _json({"zones": zones, "utc_now": now_utc.isoformat(), "count": len(zones)})


@app.get("/api/national-debt")
async def get_national_debt():
    total_debt = sum(d.get("debt_t", 0) for d in NATIONAL_DEBT)
    avg_debt_gdp = round(sum(d.get("debt_gdp_pct", 0) for d in NATIONAL_DEBT) / max(1, len(NATIONAL_DEBT)), 1)
    return _json({
        "debts": NATIONAL_DEBT,
        "total_debt_t": round(total_debt, 1),
        "avg_debt_gdp_pct": avg_debt_gdp,
        "count": len(NATIONAL_DEBT),
    })


@app.get("/api/cross-dashboard")
async def get_cross_dashboard():
    data = await cross_dashboard.fetch_all()
    return _json(data)


# ── Analyst Mode ────────────────────────────────────────────────────────────
# Entity-centric event store powering the timeline strip, dossier drawer,
# link-analysis graph, and pinboards. Backed by analyst_db (SQLite).

def _parse_csv_param(s: str | None) -> list[str]:
    if not s:
        return []
    return [p.strip() for p in s.split(",") if p.strip()]


def _parse_bbox(s: str | None):
    if not s:
        return None
    try:
        parts = [float(x) for x in s.split(",")]
        if len(parts) != 4:
            return None
        return (parts[0], parts[1], parts[2], parts[3])
    except ValueError:
        return None


def _parse_float(s: str | None) -> float | None:
    if s in (None, ""):
        return None
    try:
        return float(s)
    except ValueError:
        return None


@app.get("/api/analyst/events")
async def analyst_events(request: Request):
    qp = request.query_params
    fetch_news()  # ensure ingestion has run at least once
    events = analyst_db.query_events(
        since=_parse_float(qp.get("since")),
        until=_parse_float(qp.get("until")),
        types=_parse_csv_param(qp.get("types")) or None,
        actor_id=qp.get("actor") or None,
        bbox=_parse_bbox(qp.get("bbox")),
        limit=min(500, int(qp.get("limit") or 200)),
    )
    return _json({"events": events, "count": len(events)})


@app.get("/api/analyst/event/{event_id}")
async def analyst_event(event_id: int):
    ev = analyst_db.get_event(event_id)
    if not ev:
        return JSONResponse({"error": "not found"}, status_code=404)
    return _json(ev)


@app.get("/api/analyst/entity/{entity_id:path}")
async def analyst_entity(entity_id: str, request: Request):
    fetch_news()
    ent = analyst_db.get_entity(entity_id)
    if not ent:
        return JSONResponse({"error": "not found"}, status_code=404)
    qp = request.query_params
    recent = analyst_db.query_events(
        actor_id=entity_id,
        since=_parse_float(qp.get("since")),
        limit=min(200, int(qp.get("limit") or 50)),
    )
    graph = analyst_db.entity_graph(entity_id, depth=1, limit_per_hop=15)
    return _json({"entity": ent, "events": recent, "graph": graph})


@app.get("/api/analyst/entities")
async def analyst_search_entities(request: Request):
    q = (request.query_params.get("q") or "").strip()
    if len(q) < 1:
        return _json({"entities": []})
    results = analyst_db.search_entities(q, limit=20)
    return _json({"entities": results})


@app.get("/api/analyst/timeline")
async def analyst_timeline(request: Request):
    qp = request.query_params
    fetch_news()
    now = time.time()
    since = _parse_float(qp.get("since")) or (now - 7 * 24 * 3600)
    until = _parse_float(qp.get("until")) or now
    bucket = max(60, int(qp.get("bucket") or 3600))
    bbox = _parse_bbox(qp.get("bbox"))
    return _json(analyst_db.timeline_buckets(since, until, bucket, bbox))


@app.get("/api/analyst/graph/{entity_id:path}")
async def analyst_graph(entity_id: str, request: Request):
    qp = request.query_params
    depth = max(1, min(2, int(qp.get("depth") or 1)))
    return _json(analyst_db.entity_graph(entity_id, depth=depth))


@app.get("/api/analyst/pinboards")
async def analyst_list_pinboards():
    return _json({"pinboards": analyst_db.list_pinboards()})


@app.post("/api/analyst/pinboards")
async def analyst_create_pinboard(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    name = (body.get("name") or "").strip()[:120]
    filters = body.get("filters") or {}
    if not name or not isinstance(filters, dict):
        return JSONResponse({"error": "name and filters required"}, status_code=400)
    pin_id = analyst_db.create_pinboard(name, filters)
    return _json({"id": pin_id})


@app.delete("/api/analyst/pinboards/{pin_id}")
async def analyst_delete_pinboard(pin_id: int):
    deleted = analyst_db.delete_pinboard(pin_id)
    return _json({"deleted": deleted})


@app.get("/api/analyst/stats")
async def analyst_stats():
    fetch_news()  # warm
    return _json(analyst_db.stats())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=7050)

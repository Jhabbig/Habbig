#!/usr/bin/env python3
"""Religion & Cults Tracker — Flask backend.

Tracks the global religious landscape and a curated watchlist of new
religious movements (NRMs) and notable cults, with live signal from the
Polymarket religion-tagged markets and public news RSS feeds.

Endpoints:
  GET /                  → static index.html
  GET /api/health        → liveness
  GET /api/summary       → page-load payload (totals + freedom counts)
  GET /api/religions     → world religions adherent counts + sub-traditions
  GET /api/cults         → curated NRM / cult watchlist
  GET /api/freedom       → USCIRF designations (CPC / SWL / EPC)
  GET /api/markets       → Polymarket religion-related markets (live)
  GET /api/news          → aggregated religion news (RSS, live)

The world-religion and cult datasets are static and curated — see
religion_data.py for sourcing notes. Markets and news are fetched live
with TTL caching (5 min for markets, 10 min for news).
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
from defusedxml import ElementTree as DET
from flask import Flask, jsonify, request, send_from_directory

import acled_client
import actuarial
import alerts
import cardinals as cd
import edge as edge_calc
import health_signals
import historical_leaders as hl
import i18n
import reddit_sentinel
import religion_data as rd
import religious_calendar as rcal
import vatican_scraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("religion")

app = Flask(__name__, static_folder="static")

try:
    from flask_compress import Compress
    Compress(app)
except Exception:
    log.warning("flask_compress not available; responses will not be gzipped")

PORT = int(os.environ.get("PORT", "7062"))
HTML_PATH = Path(__file__).parent / "index.html"

# ─── Cache ────────────────────────────────────────────────────────────────────

_cache: "OrderedDict[str, dict]" = OrderedDict()
_cache_lock = threading.Lock()

_TTL_DEFAULT = 60 * 60
_TTL = {
    "polymarket": 60 * 5,
    "news":       60 * 10,
}


def cache_get(key: str):
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        ttl = _TTL.get(key, _TTL_DEFAULT)
        if time.time() - entry["t"] > ttl:
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        return entry["data"]


def cache_set(key: str, data) -> None:
    with _cache_lock:
        _cache[key] = {"t": time.time(), "data": data}
        while len(_cache) > 32:
            _cache.popitem(last=False)


# ─── HTTP helper ──────────────────────────────────────────────────────────────

_USER_AGENT = "religion-dashboard/1.0 (+https://religion.narve.ai)"


def _http_get(url: str, *, timeout: int = 12, params: Optional[dict] = None) -> Optional[requests.Response]:
    try:
        r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": _USER_AGENT})
        if r.status_code == 200:
            return r
        log.warning("HTTP %d for %s", r.status_code, url)
        return None
    except Exception as e:
        log.warning("HTTP error for %s: %s", url, e)
        return None


# ─── Polymarket fetcher ───────────────────────────────────────────────────────

GAMMA_BASE = "https://gamma-api.polymarket.com"


def _fetch_events_by_tag(tag_slug: str, seen: set, out: list, lock: threading.Lock) -> None:
    offset = 0
    for _ in range(6):
        r = _http_get(
            f"{GAMMA_BASE}/events",
            params={"tag_slug": tag_slug, "closed": "false", "limit": "100", "offset": str(offset)},
        )
        if not r:
            break
        try:
            events = r.json()
        except Exception:
            break
        if not events:
            break
        for ev in events:
            title = (ev.get("title") or "")
            tl = title.lower()
            if any(k in tl for k in rd.POLYMARKET_REJECT):
                continue
            tags = ev.get("tags", [])
            tag_labels = [t.get("label", "") for t in tags if isinstance(t, dict)]
            for m in ev.get("markets", []):
                mid = m.get("conditionId") or m.get("id") or ""
                if not mid:
                    continue
                with lock:
                    if mid in seen:
                        continue
                    seen.add(mid)
                    m["_event_title"] = title
                    m["_event_tags"] = tag_labels
                    out.append(m)
        offset += 100


def _safe_float(x) -> Optional[float]:
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _normalize_market(m: dict) -> Optional[dict]:
    """Trim a Polymarket market record to the fields we render."""
    question = (m.get("question") or m.get("_event_title") or "").strip()
    if not question:
        return None
    # Pull the YES side price. Polymarket exposes this as outcomePrices ('["0.30","0.70"]')
    # or lastTradePrice, depending on the endpoint version. Try both.
    yes_price = None
    op = m.get("outcomePrices")
    if isinstance(op, list) and op:
        yes_price = _safe_float(op[0])
    elif isinstance(op, str) and op.startswith("["):
        try:
            import json as _json
            arr = _json.loads(op)
            if arr:
                yes_price = _safe_float(arr[0])
        except Exception:
            pass
    if yes_price is None:
        yes_price = _safe_float(m.get("lastTradePrice"))
    volume = _safe_float(m.get("volumeNum")) or _safe_float(m.get("volume")) or 0.0
    liquidity = _safe_float(m.get("liquidityNum")) or _safe_float(m.get("liquidity")) or 0.0
    end_iso = m.get("endDate") or m.get("endDateIso") or ""
    slug = m.get("slug") or ""
    return {
        "id": m.get("conditionId") or m.get("id"),
        "question": question,
        "event_title": m.get("_event_title", ""),
        "yes_price": yes_price,
        "volume": volume,
        "liquidity": liquidity,
        "end_date": end_iso,
        "url": f"https://polymarket.com/event/{slug}" if slug else "",
        "tags": m.get("_event_tags", []),
    }


def fetch_markets() -> list[dict]:
    cached = cache_get("polymarket")
    if cached is not None:
        return cached
    seen: set = set()
    raw: list = []
    lock = threading.Lock()
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_fetch_events_by_tag, slug, seen, raw, lock) for slug in rd.POLYMARKET_TAG_SLUGS]
        for f in futures:
            try:
                f.result()
            except Exception as e:
                log.warning("tag fetch error: %s", e)

    # Strict keyword filter — Polymarket's tag set is noisy and bleeds into
    # politics + crypto. Require a religion-keyword in either the market
    # question or the parent event title.
    out: list[dict] = []
    for m in raw:
        question = (m.get("question") or "")
        ev_title = (m.get("_event_title") or "")
        blob = (question + " " + ev_title).lower()
        if not any(k in blob for k in rd.POLYMARKET_KEYWORDS):
            continue
        norm = _normalize_market(m)
        if norm:
            out.append(norm)

    out.sort(key=lambda x: x.get("volume") or 0, reverse=True)
    cache_set("polymarket", out)
    return out


# ─── News RSS fetcher ────────────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    return _TAG_RE.sub("", s or "").strip()


def _parse_rss(xml: str, source: str) -> list[dict]:
    out: list[dict] = []
    try:
        root = DET.fromstring(xml)
    except Exception as e:
        log.warning("RSS parse failed for %s: %s", source, e)
        return out

    # Strip namespaces from tags so simple .find() works regardless of prefix.
    def _local(tag: str) -> str:
        return tag.split("}", 1)[1] if "}" in tag else tag

    # Walk RSS 2.0 (channel/item) and Atom (feed/entry).
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        title = ""
        link = ""
        published = ""
        summary = ""
        for child in el:
            t = _local(child.tag)
            text = (child.text or "").strip()
            if t == "title" and not title:
                title = text
            elif t == "link" and not link:
                link = text or child.attrib.get("href", "")
            elif t in ("pubDate", "published", "updated", "date") and not published:
                published = text
            elif t in ("description", "summary", "content") and not summary:
                summary = _strip_html(text)
        if not title or not link:
            continue
        out.append({
            "source": source,
            "title": title,
            "link": link,
            "published": published,
            "summary": (summary[:280] + "…") if len(summary) > 280 else summary,
        })
    return out


def fetch_news() -> list[dict]:
    cached = cache_get("news")
    if cached is not None:
        return cached
    items: list[dict] = []
    from concurrent.futures import ThreadPoolExecutor

    def _one(name: str, url: str) -> list[dict]:
        r = _http_get(url, timeout=10)
        if not r:
            return []
        return _parse_rss(r.text, name)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_one, name, url) for name, url in rd.NEWS_RSS_FEEDS]
        for f in futures:
            try:
                items.extend(f.result())
            except Exception as e:
                log.warning("news fetch error: %s", e)

    # De-duplicate by link.
    seen: set = set()
    deduped: list[dict] = []
    for it in items:
        if it["link"] in seen:
            continue
        seen.add(it["link"])
        deduped.append(it)

    # Sort newest-first when we can parse the date.
    def _ts(it: dict) -> float:
        p = it.get("published") or ""
        for fmt in (
            "%a, %d %b %Y %H:%M:%S %z",
            "%a, %d %b %Y %H:%M:%S %Z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%SZ",
        ):
            try:
                return datetime.strptime(p, fmt).timestamp()
            except ValueError:
                continue
        return 0.0

    deduped.sort(key=_ts, reverse=True)
    deduped = deduped[:60]
    cache_set("news", deduped)
    return deduped


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if not HTML_PATH.exists():
        return ("index.html missing", 500)
    return HTML_PATH.read_text(encoding="utf-8"), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/static/<path:path>")
def static_files(path):
    return send_from_directory(app.static_folder, path)


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "service": "religion-dashboard"})


@app.route("/api/religions")
def api_religions():
    return jsonify({
        "fetched_at": int(time.time()),
        "source": "Pew Research Center, baseline 2020 estimates",
        "religions": rd.WORLD_RELIGIONS,
        "subgroups": rd.RELIGION_SUBGROUPS,
    })


@app.route("/api/religions-full")
def api_religions_full():
    """100-tradition registry (denominations / sects / movements)."""
    family = (request.args.get("family") or "").strip()
    q = (request.args.get("q") or "").strip().lower()
    items = list(rd.RELIGIONS_FULL)
    if family:
        items = [r for r in items if r.get("family") == family]
    if q:
        items = [r for r in items
                 if q in r["name"].lower()
                 or q in r["origin"].lower()
                 or q in r["summary"].lower()]
    families = sorted({r["family"] for r in rd.RELIGIONS_FULL})
    return jsonify({
        "fetched_at": int(time.time()),
        "source": "Pew, World Religion Database, ARDA, Britannica + official censuses",
        "count": len(items),
        "total": len(rd.RELIGIONS_FULL),
        "families": families,
        "religions": items,
    })


@app.route("/api/cults")
def api_cults():
    status = (request.args.get("status") or "").strip().lower()
    risk = (request.args.get("risk") or "").strip().lower()
    items = []
    for c in rd.CULTS_WATCHLIST:
        score, bucket = rd.cult_risk_score(c)
        items.append({**c, "risk_score": score, "risk": bucket})
    if status:
        items = [c for c in items if status in (c.get("status") or "").lower()]
    if risk:
        items = [c for c in items if c["risk"] == risk]
    items.sort(key=lambda c: c["risk_score"], reverse=True)
    return jsonify({
        "fetched_at": int(time.time()),
        "count": len(items),
        "groups": items,
        "scoring_axes": ["financial_opacity", "leadership_risk", "isolation", "criminal_disclosure"],
    })


@app.route("/api/leaders")
def api_leaders():
    """Religious leaders with life-table actuarial. ?ref=YYYY-MM-DD overrides today."""
    ref_str = (request.args.get("ref") or "").strip()
    try:
        ref = date.fromisoformat(ref_str) if ref_str else date.today()
    except ValueError:
        ref = date.today()

    out = []
    for L in rd.RELIGIOUS_LEADERS:
        born = L.get("born") or ""
        try:
            age = actuarial.age_on(born, ref)
        except Exception:
            age = None
        actu = None
        if age is not None and age > 0:
            sex = L.get("sex", "M")
            hr = hl.MORTALITY_HAZARD_RATIO_RELIGIOUS
            actu = {
                "age": round(age, 2),
                # SSA baseline
                "p_alive_1y":  round(actuarial.survival_prob(age, sex,  12), 4),
                "p_alive_5y":  round(actuarial.survival_prob(age, sex,  60), 4),
                "p_alive_10y": round(actuarial.survival_prob(age, sex, 120), 4),
                "p_dies_1y":   round(1 - actuarial.survival_prob(age, sex, 12),  4),
                # Religious-office adjusted (HR = 0.85, see historical_leaders.py)
                "p_alive_1y_adj":  round(actuarial.survival_prob(age, sex,  12, hazard_ratio=hr), 4),
                "p_alive_5y_adj":  round(actuarial.survival_prob(age, sex,  60, hazard_ratio=hr), 4),
                "p_alive_10y_adj": round(actuarial.survival_prob(age, sex, 120, hazard_ratio=hr), 4),
                "p_dies_1y_adj":   round(1 - actuarial.survival_prob(age, sex, 12, hazard_ratio=hr), 4),
            }
        # Years in office
        took = L.get("took_office") or ""
        try:
            years_in_office = round(actuarial.age_on(took, ref), 1) if took else None
        except Exception:
            years_in_office = None

        out.append({**L, "actuarial": actu, "years_in_office": years_in_office})

    # Sort: highest 1-year death probability first (most market-relevant).
    out.sort(key=lambda x: (x["actuarial"]["p_dies_1y"] if x.get("actuarial") else -1), reverse=True)

    # Attach per-leader news (matched by surname in title/summary)
    if request.args.get("news", "1") != "0":
        try:
            news = fetch_news()
            out = _attach_leader_news(out, news)
        except Exception as e:
            log.warning("leader news attach failed: %s", e)

    return jsonify({
        "fetched_at": int(time.time()),
        "ref_date": ref.isoformat(),
        "model": "SSA 2022 period life table; per-month survival walk forward.",
        "adjusted_hazard_ratio": hl.MORTALITY_HAZARD_RATIO_RELIGIOUS,
        "adjustment_basis": f"{hl.COHORT_SIZE} historical religious leaders, mean age at death {hl.COHORT_MEAN_AGE_AT_DEATH:.1f}y",
        "count": len(out),
        "leaders": out,
    })


@app.route("/api/conclave")
def api_conclave():
    """College of Cardinals + papabile priors + aggregate stats. The flagship."""
    region = (request.args.get("region") or "").strip()
    wing = (request.args.get("wing") or "").strip()
    only_electors = request.args.get("electors") in ("1", "true", "yes")
    only_papabile = request.args.get("papabile") in ("1", "true", "yes")

    cards = list(cd.CARDINALS)
    if region:
        cards = [c for c in cards if c.get("region") == region]
    if wing:
        cards = [c for c in cards if c.get("wing") == wing]
    if only_electors:
        cards = [c for c in cards if c.get("elector")]
    if only_papabile:
        cards = [c for c in cards if (c.get("papabile_tier") or 0) >= 2]

    # Sort: papabile_tier desc, then age asc (younger = more conclave value)
    cards.sort(key=lambda c: (-(c.get("papabile_tier") or 0), c.get("age") or 999))

    # Compute sample breakdowns for the filtered set
    from collections import Counter
    sample_breakdown = {
        "by_region":    dict(Counter(c["region"] for c in cards)),
        "by_appointer": dict(Counter(c["appointed_by"] for c in cards)),
        "by_wing":      dict(Counter(c["wing"] for c in cards)),
    }

    return jsonify({
        "fetched_at": int(time.time()),
        "source": "Vatican Press Office, College of Cardinals Report, Vaticanist press consensus",
        "sample_size": len(cards),
        "cardinals": cards,
        "papabile_priors": cd.PAPABILE_PRIORS,
        "rules": cd.CONCLAVE_RULES,
        "college_aggregates": cd.COLLEGE_AGGREGATES,
        "sample_breakdown": sample_breakdown,
    })


@app.route("/api/conclave/live")
def api_conclave_live():
    """Live College of Cardinals from press.vatican.va, merged with curated metadata.

    Falls back to curated CARDINALS when the scrape fails. The response
    distinguishes 'live' (fresh scrape) from 'stale-cache' (last good
    scrape, still serving) from 'fallback-curated' (never reached Vatican).
    """
    force = request.args.get("force") in ("1", "true", "yes")
    result = vatican_scraper.fetch_full_college(force=force)

    scraped = result.get("cardinals") or []
    if scraped:
        merged = vatican_scraper.merge_with_curated(scraped, cd.CARDINALS)
        # Sort: papabile_tier desc, then age asc
        merged.sort(key=lambda c: (-(c.get("papabile_tier") or 0), c.get("age") or 999))
        drift = vatican_scraper.detect_drift(scraped, cd.CARDINALS)
        source = "live" if result["ok"] else "stale-cache"
    else:
        # Fallback: serve curated data with scrape metadata indicating failure
        merged = list(cd.CARDINALS)
        drift = {"added_since_curated": [], "missing_from_scraped": [], "scraped_count": 0, "curated_count": len(merged)}
        source = "fallback-curated"

    age_seconds = int(time.time() - result["fetched_at"]) if result.get("fetched_at") else None
    return jsonify({
        "source": source,
        "fetched_at": result.get("fetched_at"),
        "age_seconds": age_seconds,
        "error": result.get("error", ""),
        "vatican_url": vatican_scraper.CARDINALS_LIST_URL,
        "scraped_count": len(scraped),
        "curated_count": len(cd.CARDINALS),
        "drift": drift,
        "cardinals": merged,
        "papabile_priors": cd.PAPABILE_PRIORS,
        "rules": cd.CONCLAVE_RULES,
        "college_aggregates": cd.COLLEGE_AGGREGATES,
    })


@app.route("/api/historical-leaders")
def api_historical_leaders():
    """Cohort used to calibrate the religious-leader hazard ratio."""
    return jsonify({
        "fetched_at": int(time.time()),
        "count": hl.COHORT_SIZE,
        "mean_age_at_death": round(hl.COHORT_MEAN_AGE_AT_DEATH, 2),
        "hazard_ratio": hl.MORTALITY_HAZARD_RATIO_RELIGIOUS,
        "leaders": hl.HISTORICAL_LEADERS_DECEASED,
    })


@app.route("/api/countries")
def api_countries():
    """Top countries by population with religion composition. Optional ?religion= filter."""
    rel = (request.args.get("religion") or "").strip()
    items = list(rd.COUNTRY_RELIGION)
    if rel:
        items = [c for c in items if c.get("majority") == rel]
    items.sort(key=lambda c: c.get("pop_m") or 0, reverse=True)
    # Build a quick "by religion" rollup
    rollup: dict[str, dict] = {}
    for c in rd.COUNTRY_RELIGION:
        for r, pct in (c.get("religion_pct") or {}).items():
            adherents_m = (c.get("pop_m") or 0) * (pct / 100.0)
            rollup.setdefault(r, {"adherents_m": 0.0, "countries": 0})
            rollup[r]["adherents_m"] += adherents_m
            rollup[r]["countries"] += 1
    for r in rollup:
        rollup[r]["adherents_m"] = round(rollup[r]["adherents_m"], 1)
    return jsonify({
        "fetched_at": int(time.time()),
        "source": "Pew Research Center, Religious Composition by Country (2010-2050 series)",
        "count": len(items),
        "countries": items,
        "rollup_by_religion": rollup,
    })


@app.route("/api/calendar")
def api_calendar():
    """Multi-year religious calendar generator (Meeus Easter + lookup tables).

    Default year = current year. Supports 2025-2034 (lookup tables); Easter
    + dependents are algorithmic and work indefinitely.

    Filters:
        ?year=N         — pick a calendar year
        ?upcoming=1     — only events ≥ today
        ?days=N         — cap horizon at N days from today (1-1095)
    """
    upcoming_only = request.args.get("upcoming") in ("1", "true", "yes")
    horizon = int(request.args.get("days") or "365")
    horizon = max(1, min(horizon, 1095))   # up to 3 years
    today = date.today()
    try:
        year = int(request.args.get("year") or today.year)
    except ValueError:
        year = today.year

    # If asking for upcoming + horizon spans multiple years, generate the
    # union to avoid hiding events that fall after Dec 31.
    years = {year}
    if upcoming_only and horizon > 0:
        end_date = today + timedelta(days=horizon)
        years.add(end_date.year)
    items: list[dict] = []
    for y in sorted(years):
        items.extend(rcal.generate_calendar(y))

    # De-dupe by (date, name)
    seen = set()
    deduped = []
    for e in items:
        key = (e["date"], e["name"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)

    if upcoming_only:
        deduped = [e for e in deduped if date.fromisoformat(e["date"]) >= today]
        # Horizon filter only applies in upcoming mode (otherwise the user
        # explicitly asked for a year and we should return that whole year).
        deduped = [e for e in deduped if (date.fromisoformat(e["date"]) - today).days <= horizon]
    deduped.sort(key=lambda e: e["date"])

    return jsonify({
        "fetched_at": int(time.time()),
        "today": today.isoformat(),
        "year": year,
        "supported_years": rcal.get_supported_years(),
        "count": len(deduped),
        "events": deduped,
    })


@app.route("/api/freedom")
def api_freedom():
    return jsonify({
        "fetched_at": int(time.time()),
        "source": "USCIRF Annual Report 2024",
        "designations": rd.USCIRF_2024,
        "counts": {
            "cpc": len(rd.USCIRF_2024["cpc"]),
            "swl": len(rd.USCIRF_2024["swl"]),
            "epc": len(rd.USCIRF_2024["epc"]),
        },
    })


@app.route("/api/markets")
def api_markets():
    markets = fetch_markets()
    return jsonify({
        "fetched_at": int(time.time()),
        "count": len(markets),
        "markets": markets,
    })


def _attach_leader_news(leaders: list[dict], news: list[dict]) -> list[dict]:
    """For each leader, attach news items whose title or summary mentions them.

    Matching: case-insensitive substring of either the full name or any
    surname-token of length >= 4. Caps at 5 most-recent items per leader.
    """
    out = []
    for L in leaders:
        keys = [L["name"].lower()]
        # Surname tokens > 3 chars (e.g. "Khamenei", "Sistani", "Dalai", "Lama")
        for tok in L["name"].split():
            t = tok.lower().strip(".,()")
            if len(t) >= 4 and t not in ("pope", "the", "saint", "card"):
                keys.append(t)
        matched = []
        seen_links = set()
        for n in news:
            blob = ((n.get("title") or "") + " " + (n.get("summary") or "")).lower()
            if any(k in blob for k in keys):
                link = n.get("link", "")
                if link in seen_links:
                    continue
                seen_links.add(link)
                matched.append({
                    "title": n.get("title", ""),
                    "link":  link,
                    "source": n.get("source", ""),
                    "published": n.get("published", ""),
                })
                if len(matched) >= 5:
                    break
        out.append({**L, "news": matched, "news_count": len(matched)})
    return out


def _require_alerts_auth() -> Optional[tuple]:
    """Return None when authorised, else (response, status)."""
    expected = "Bearer " + alerts.HMAC_SECRET
    if not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
        return (jsonify({"error": "unauthorised — supply Authorization: Bearer <ALERTS_HMAC_SECRET>"}), 401)
    return None


@app.route("/api/alerts", methods=["POST"])
def api_alerts_subscribe():
    err = _require_alerts_auth()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    url = (body.get("webhook_url") or "").strip()
    conds = body.get("conditions") or []
    label = body.get("label") or ""
    if not url:
        return jsonify({"ok": False, "error": "webhook_url required"}), 400
    result = alerts.subscribe(url, conds, label)
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/alerts", methods=["GET"])
def api_alerts_list():
    err = _require_alerts_auth()
    if err:
        return err
    return jsonify({"subscriptions": alerts.list_subscriptions()})


@app.route("/api/alerts/<int:sub_id>", methods=["DELETE"])
def api_alerts_delete(sub_id: int):
    err = _require_alerts_auth()
    if err:
        return err
    return jsonify(alerts.delete_subscription(sub_id))


def _snapshot_for_alerts() -> dict:
    """Snapshot the model outputs the alert engine inspects."""
    from datetime import date as _date
    try:
        ph = health_signals.compute_health_signal(fetch_news(), today=_date.today(), window_days=14)
    except Exception as e:
        log.warning("alerts snapshot: pope_health failed: %s", e)
        ph = None
    try:
        markets = fetch_markets()
        ranked = edge_calc.rank_markets_by_edge(markets, rd.RELIGIOUS_LEADERS, cd.PAPABILE_PRIORS,
                                                today=_date.today())
        edge_snap = {"markets": ranked}
    except Exception as e:
        log.warning("alerts snapshot: edge failed: %s", e)
        edge_snap = None
    try:
        vat = vatican_scraper.fetch_full_college()
        drift = vatican_scraper.detect_drift(vat.get("cardinals") or [], cd.CARDINALS) if vat.get("ok") else {}
        conclave_snap = {"source": "live" if vat.get("ok") else "fallback-curated", "drift": drift}
    except Exception as e:
        log.warning("alerts snapshot: conclave failed: %s", e)
        conclave_snap = None
    return {"pope_health": ph, "edge": edge_snap, "conclave_drift": conclave_snap}


def _alerts_loop() -> None:
    """Background thread: periodically run all subscription conditions."""
    log.info("alerts loop started (interval %ds)", alerts.CHECK_INTERVAL_SECONDS)
    while True:
        time.sleep(alerts.CHECK_INTERVAL_SECONDS)
        try:
            snap = _snapshot_for_alerts()
            report = alerts.run_check_cycle(snap)
            if report["fired"]:
                log.info("alerts cycle: %s", report)
        except Exception as e:
            log.warning("alerts loop error: %s", e)


# Start the background thread once when the module is imported by the Flask
# app. Daemon=True so it exits with the process.
_alerts_thread_started = False
def _start_alerts_thread_once() -> None:
    global _alerts_thread_started
    if _alerts_thread_started:
        return
    if os.environ.get("DISABLE_ALERTS") == "1":
        return
    t = threading.Thread(target=_alerts_loop, name="alerts-loop", daemon=True)
    t.start()
    _alerts_thread_started = True


@app.route("/api/i18n")
def api_i18n():
    """Return the full UI translation dictionary for the requested language.

    ?lang=es|it|fr|pt|en (default en). Missing keys fall back to English.
    """
    lang = (request.args.get("lang") or "en").strip().lower()
    if lang not in i18n.available_languages():
        lang = "en"
    return jsonify({
        "lang": lang,
        "available": i18n.available_languages(),
        "strings": i18n.all_strings(lang),
    })


@app.route("/api/violence")
def api_violence():
    """ACLED religious-violence events for the past N days.

    Requires ACLED_EMAIL + ACLED_API_KEY env vars in production. Falls
    back to ok=False + empty list if creds are missing or API fails.
    """
    try:
        days_back = max(1, min(int(request.args.get("days", "30")), 365))
    except ValueError:
        days_back = 30
    force = request.args.get("force") in ("1", "true", "yes")
    return jsonify(acled_client.fetch_recent_violence(days_back=days_back, force=force))


@app.route("/api/cult-sentinel")
def api_cult_sentinel():
    """Public-Reddit emerging-group mentions (read-only)."""
    force = request.args.get("force") in ("1", "true", "yes")
    return jsonify(reddit_sentinel.fetch_sentinel(force=force))


@app.route("/api/pope-health")
def api_pope_health():
    """Lexical health-signal scorer for the current Pope.

    Aggregates news items in the past N days (default 14) for phrases
    indicating hospitalisation, cancelled audiences, illness, etc.
    Returns a 0-10 score with band classification.
    """
    from datetime import date as _date
    window = max(1, min(int(request.args.get("days", "14")), 60))
    news = fetch_news()
    result = health_signals.compute_health_signal(news, today=_date.today(), window_days=window)
    return jsonify({**result, "fetched_at": int(time.time())})


@app.route("/api/edge")
def api_edge():
    """Polymarket markets ranked by edge against our quantitative models.

    Each market is matched (when possible) against one of:
      - Leader actuarial (P(alive)/P(dies) for tracked religious leaders)
      - Vacancy of the Holy See
      - Papabile prior (P(this cardinal is next Pope))

    Returns markets sorted by absolute edge in percentage points; unmatched
    markets fall to the bottom and are ranked by volume.
    """
    from datetime import date as _date
    markets = fetch_markets()
    today = _date.today()
    ranked = edge_calc.rank_markets_by_edge(
        markets, rd.RELIGIOUS_LEADERS, cd.PAPABILE_PRIORS, today=today,
    )
    matched = [m for m in ranked if m.get("edge_pp") is not None]
    return jsonify({
        "fetched_at": int(time.time()),
        "today": today.isoformat(),
        "total_markets": len(ranked),
        "matched_markets": len(matched),
        "model_hazard_ratio": hl.MORTALITY_HAZARD_RATIO_RELIGIOUS,
        "markets": ranked,
    })


@app.route("/api/news")
def api_news():
    items = fetch_news()
    return jsonify({
        "fetched_at": int(time.time()),
        "count": len(items),
        "items": items,
    })


@app.route("/api/summary")
def api_summary():
    """Single payload for the page header — totals only, no live calls."""
    total_m = sum(r["adherents_m"] for r in rd.WORLD_RELIGIONS)
    cults = rd.CULTS_WATCHLIST
    return jsonify({
        "fetched_at": int(time.time()),
        "world_population_m": total_m,
        "religions_tracked": len(rd.WORLD_RELIGIONS),
        "registry_size": len(rd.RELIGIONS_FULL),
        "leaders_tracked": len(rd.RELIGIOUS_LEADERS),
        "cardinals_profiled": len(cd.CARDINALS),
        "papabile_tracked": len(cd.PAPABILE_PRIORS),
        "countries_tracked": len(rd.COUNTRY_RELIGION),
        "calendar_events": len(rd.RELIGIOUS_CALENDAR_2026),
        "cults_tracked": len(cults),
        "cults_active": sum(1 for c in cults if "active" in (c.get("status") or "").lower()),
        "cults_defunct": sum(1 for c in cults if (c.get("status") or "").lower() == "defunct"),
        "cpc_countries": len(rd.USCIRF_2024["cpc"]),
        "swl_countries": len(rd.USCIRF_2024["swl"]),
        "epc_entities": len(rd.USCIRF_2024["epc"]),
    })


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("religion-dashboard listening on :%d", PORT)
    _start_alerts_thread_once()
    app.run(host=os.environ.get("BIND_HOST", "0.0.0.0"), port=PORT, threaded=True)
else:
    _start_alerts_thread_once()

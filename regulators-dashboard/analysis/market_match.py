"""Match RSS action items to Polymarket / Kalshi markets.

Strategy:
  1. Tokenize both the item (title + summary) and the market (question)
     into lowercased alphanumeric tokens, dropping stopwords and tokens
     shorter than 3 characters.
  2. An "anchor" token must appear in the intersection: a regulator code
     (SEC, FCA, ESMA, CFTC, FinCEN, OFAC, DOJ, …) or a marquee topic
     keyword (ETF, stablecoin, named exchange, named regulator chair).
     The anchor guard is what prevents "rules" + "rules" + "the" + "the"
     from being a match.
  3. Score = Jaccard similarity (|A ∩ B| / |A ∪ B|).
  4. Surface matches above `MATCH_THRESHOLD` (default 0.15). The top 3 by
     score attach to each item.

The matcher is a pure function — no I/O, no cache. It takes the cached
items list and the cached markets list and returns a join. The caller
runs this on every `/api/feed` request so market prices are always
5-min-fresh (the underlying clients cache).

False-positive shape we DO accept: the matcher will surface markets that
are *topically* related but resolve on a different specific event. The UI
shows the full market question + deep-link, so the user verifies on the
venue before acting. That's the same posture as `centralbank-dashboard`
v0.5 — read-only on trade, optimistic on match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Regulator codes + marquee topic words. Lowercase. These are tokens that,
# when shared between an item and a market, justify treating the overlap as
# a real signal. Without an anchor, two press releases mentioning "rules"
# and "the" would otherwise hit a non-trivial Jaccard.
ANCHOR_TOKENS: set[str] = {
    # Regulators
    "sec", "fca", "esma", "cftc", "fincen", "ofac", "bafin", "finma",
    "mas", "asic", "jfsa", "hkma", "doj", "fed", "fdic", "occ", "pra", "eba",
    # Topics
    "etf", "etfs", "stablecoin", "stablecoins",
    "bitcoin", "ethereum", "btc", "eth", "solana", "sol", "xrp",
    "binance", "coinbase", "kraken", "ftx", "tether",
    "aml", "kyc", "fatf",
    "climate", "esg", "carbon", "emissions",
    "ico", "nft", "defi", "mica",
    "ransomware", "cybersecurity",
    # Notable people who anchor regulator-themed markets
    "powell", "gensler", "atkin", "atkins", "uyeda",
}

STOPWORDS: set[str] = {
    "the", "and", "for", "with", "from", "into", "that", "this",
    "will", "have", "has", "had", "are", "was", "were", "be", "been",
    "not", "but", "also", "more", "than", "such", "any", "all",
    "new", "now", "may", "would", "could", "should",
    "their", "they", "them", "your", "you", "our", "its", "his", "her",
    "rule", "rules", "act", "section", "today", "announces", "announced",
    "release", "press", "official", "statement", "publishes", "published",
    "year", "years", "first", "next", "last", "ago",
    # Common in market questions
    "before", "after", "during", "between", "above", "below",
    "approve", "approves", "approved", "approval",
    "decision", "outcome", "winner",
}


_TOKEN_RX = re.compile(r"\b[a-z][a-z0-9]+\b")


def tokenize(text: str) -> set[str]:
    if not text:
        return set()
    return {
        t for t in _TOKEN_RX.findall(text.lower())
        if t not in STOPWORDS and len(t) >= 3
    }


MATCH_THRESHOLD = 0.18
MIN_INTERSECTION = 2
MAX_MATCHES_PER_ITEM = 3


@dataclass
class _PreparedMarket:
    market: dict
    tokens: set[str]


def prepare_markets(markets: list[dict]) -> list[_PreparedMarket]:
    """Pre-tokenize once so we don't redo it per item — N items × M markets
    becomes O(M) tokenization + O(N×M) set-intersect."""
    return [_PreparedMarket(market=m, tokens=tokenize(m.get("question", ""))) for m in markets]


def _format_match(market: dict, score: float, intersect: set[str]) -> dict:
    return {
        "source": market["source"],
        "question": market["question"],
        "yes_price": market.get("yes_price"),
        "no_price": market.get("no_price"),
        "end_date": market.get("end_date"),
        "url": market.get("url"),
        "match_score": round(score, 3),
        "match_tokens": sorted(intersect),
    }


def match_for_item(item: dict, prepared: list[_PreparedMarket],
                   threshold: float = MATCH_THRESHOLD,
                   max_matches: int = MAX_MATCHES_PER_ITEM) -> list[dict]:
    text = (item.get("title", "") + " " + item.get("summary", ""))
    item_tokens = tokenize(text)
    if not item_tokens:
        return []

    candidates: list[tuple[float, set[str], dict]] = []
    for pm in prepared:
        inter = item_tokens & pm.tokens
        if len(inter) < MIN_INTERSECTION:
            continue
        anchor_overlap = inter & ANCHOR_TOKENS
        # Require at least one anchor in the overlap. Without this, two
        # press-releases mentioning generic words ("market", "rule", "fund")
        # would otherwise hit a non-trivial Jaccard.
        if not anchor_overlap:
            continue
        union = item_tokens | pm.tokens
        if not union:
            continue
        # Anchor-weighted score: anchor tokens count 3× in the numerator.
        # Tuned so that:
        #   - 1 shared anchor + 1 noise token → score ≈ 0.25–0.30 (matches)
        #   - 0 shared anchors                → no match (anchor guard)
        #   - lots of shared noise, no anchor → no match (anchor guard)
        weighted_inter = len(inter) + 2 * len(anchor_overlap)
        score = weighted_inter / len(union)
        if score < threshold:
            continue
        candidates.append((score, inter, pm.market))

    candidates.sort(key=lambda x: -x[0])
    return [_format_match(m, s, i) for s, i, m in candidates[:max_matches]]


def attach_matches(items: list[dict], markets: list[dict]) -> list[dict]:
    """Return a new list of shallow-copied items each with a `markets` field
    attached. Caller passes filtered items + the combined market list."""
    prepared = prepare_markets(markets)
    out: list[dict] = []
    for it in items:
        matches = match_for_item(it, prepared)
        out.append({**it, "markets": matches})
    return out


# --- Self-test --------------------------------------------------------------

_FIXTURE_ITEMS = [
    {
        "title": "SEC charges crypto exchange",
        "summary": "Today announced a settlement with FTX over alleged unregistered securities offerings.",
    },
    {
        "title": "FCA approves first spot Bitcoin ETF",
        "summary": "Authority approves application for retail investors.",
    },
    {
        "title": "Powell delivers speech on the economy",
        "summary": "Fed Chair addressed the Economic Club on inflation.",
    },
    {
        "title": "ESMA publishes work programme",
        "summary": "Annual work programme released.",
    },
]

_FIXTURE_MARKETS = [
    {"source": "polymarket", "question": "Will the SEC charge FTX executives by end of 2026?",
     "yes_price": 0.62, "url": "https://polymarket.com/event/sec-ftx-charges"},
    {"source": "kalshi",     "question": "Will the FCA approve a spot Bitcoin ETF in 2026?",
     "yes_price": 0.41, "url": "https://kalshi.com/markets/fca-bitcoin-etf"},
    {"source": "polymarket", "question": "Will Powell be Fed Chair on Dec 31, 2026?",
     "yes_price": 0.78, "url": "https://polymarket.com/event/powell-chair"},
    {"source": "polymarket", "question": "Will the Lakers win the NBA championship?",
     "yes_price": 0.05, "url": "https://polymarket.com/event/lakers-nba"},
]

if __name__ == "__main__":
    enriched = attach_matches(_FIXTURE_ITEMS, _FIXTURE_MARKETS)
    for it in enriched:
        ms = it["markets"]
        print(f"-- {it['title']}")
        if not ms:
            print("   (no match)")
        for m in ms:
            print(f"   [{m['source']}]  score={m['match_score']}  yes={m['yes_price']}  "
                  f"shared={m['match_tokens']}")
            print(f"     {m['question']}")
    # Spot-checks
    assert any(m["question"].startswith("Will the SEC charge FTX") for m in enriched[0]["markets"])
    assert any("FCA approve" in m["question"] for m in enriched[1]["markets"])
    assert any("Powell" in m["question"] for m in enriched[2]["markets"])
    assert enriched[3]["markets"] == []  # ESMA work programme — no relevant market
    # The Lakers market must never leak through.
    for it in enriched:
        for m in it["markets"]:
            assert "Lakers" not in m["question"], f"Lakers leak: {it['title']}"
    print("\nsmoke OK")

"""Keyword & subreddit configuration mapping topics to narve.ai dashboards.

Keep the keyword lists short and high-signal. Noisy keywords (e.g. "trade",
"market") flood the queue with junk leads.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DashboardTopic:
    key: str                      # internal dashboard key in gateway/config.json
    subreddits: tuple[str, ...]   # subreddits to poll
    keywords: tuple[str, ...]     # keywords to match in title + body
    hn_query: str                 # Algolia query string for HN
    pitch: str                    # one-line pitch used in drafted copy


TOPICS: tuple[DashboardTopic, ...] = (
    DashboardTopic(
        key="crypto",
        subreddits=("CryptoCurrency", "Bitcoin", "ethtrader", "CryptoMarkets"),
        keywords=(
            "BTC prediction", "BTC signal", "crypto signals", "ML predictor",
            "ensemble model", "altcoin signal", "BTC forecast",
        ),
        hn_query="crypto prediction ensemble",
        pitch=(
            "Crypto Edge runs an ensemble ML predictor over BTC and the majors "
            "— signal, confidence interval, and the inputs feeding it."
        ),
    ),
    DashboardTopic(
        key="midterm",
        subreddits=("PredictionMarkets", "polymarket", "fivethirtyeight", "politicalbetting"),
        keywords=(
            "midterm", "election forecast", "polling aggregat", "house race",
            "senate race", "2026 election", "fivethirtyeight",
        ),
        hn_query="election polling aggregator",
        pitch=(
            "Midterm Predictor aggregates polls + Polymarket + Kalshi prices "
            "into one race-by-race view, with movement alerts."
        ),
    ),
    DashboardTopic(
        key="weather",
        subreddits=("PredictionMarkets", "polymarket", "weather", "TropicalWeather"),
        keywords=(
            "weather market", "rain prediction", "NYC rain", "weather polymarket",
            "snowfall market", "hurricane market",
        ),
        hn_query="weather prediction market",
        pitch=(
            "Polymarket Weather flags mispricings on rain/snow/hurricane "
            "markets vs the NWS forecast — typically a few % edge per week."
        ),
    ),
    DashboardTopic(
        key="sports",
        subreddits=("sportsbook", "polymarket", "SportsBetting", "PredictionMarkets"),
        keywords=(
            "polymarket vs", "sportsbook arbitrage", "odds arbitrage",
            "bookmaker edge", "polymarket sports",
        ),
        hn_query="sportsbook polymarket arbitrage",
        pitch=(
            "Sharpe Sports compares Polymarket prices to the major books in "
            "real-time and surfaces arbitrage windows."
        ),
    ),
    DashboardTopic(
        key="top_traders",
        subreddits=("polymarket", "PredictionMarkets"),
        keywords=(
            "polymarket leaderboard", "top trader", "polymarket whale",
            "copy trading polymarket",
        ),
        hn_query="polymarket leaderboard trader",
        pitch=(
            "Top Traders surfaces what Polymarket's biggest accounts are "
            "buying right now, with leaderboard movement and trade size."
        ),
    ),
    DashboardTopic(
        key="world",
        subreddits=("geopolitics", "worldnews", "PredictionMarkets"),
        keywords=(
            "geopolitical risk", "conflict tracker", "global headlines feed",
            "geopolitical dashboard",
        ),
        hn_query="geopolitical dashboard conflict tracker",
        pitch=(
            "World State is a single feed of conflict events, headlines, and "
            "the prediction markets that price them."
        ),
    ),
    DashboardTopic(
        key="climate",
        subreddits=("climate", "ClimateActionPlan", "PredictionMarkets"),
        keywords=(
            "climate market", "GISTEMP", "sea ice market", "ENSO forecast",
            "CO2 prediction",
        ),
        hn_query="climate prediction market",
        pitch=(
            "Climate Change tracks long-horizon markets on GISTEMP, sea ice, "
            "CO₂, and ENSO with the underlying data side-by-side."
        ),
    ),
    DashboardTopic(
        key="centralbank",
        subreddits=("economy", "PredictionMarkets", "investing"),
        keywords=(
            "FOMC", "implied rate path", "fed funds future", "ECB rate",
            "rate cut probability",
        ),
        hn_query="FOMC implied rate path",
        pitch=(
            "Central Bank Tracker shows Fed/ECB/BoE rates next to the "
            "implied path and the Polymarket FOMC edge."
        ),
    ),
    DashboardTopic(
        key="disasters",
        subreddits=("TropicalWeather", "earthquakes", "Wildfire", "PredictionMarkets"),
        keywords=(
            "hurricane forecast", "earthquake market", "wildfire prediction",
            "disaster market",
        ),
        hn_query="disaster prediction market",
        pitch=(
            "Major Disasters surfaces edges on hurricane, earthquake, and "
            "wildfire markets against the official forecasts."
        ),
    ),
    DashboardTopic(
        key="crypto_trackers",
        subreddits=("CryptoCurrency", "defi", "ethfinance"),
        keywords=(
            "DeFi TVL tracker", "cross-exchange spread", "funding rate dashboard",
            "fear and greed", "altcoin tracker",
        ),
        hn_query="defi tvl tracker funding rate",
        pitch=(
            "Crypto Trackers covers every coin across exchanges with funding, "
            "DeFi TVL, F&G, and a hard line on data fidelity."
        ),
    ),
)


# Flat list of subreddits to poll (deduped).
ALL_SUBREDDITS: tuple[str, ...] = tuple(sorted({s for t in TOPICS for s in t.subreddits}))


def topic_for_text(text: str) -> DashboardTopic | None:
    """Best-effort match of free text to a dashboard topic by keyword presence."""
    if not text:
        return None
    lower = text.lower()
    best: tuple[DashboardTopic, int] | None = None
    for topic in TOPICS:
        hits = sum(1 for kw in topic.keywords if kw.lower() in lower)
        if hits and (best is None or hits > best[1]):
            best = (topic, hits)
    return best[0] if best else None


def topic_by_key(key: str) -> DashboardTopic | None:
    for t in TOPICS:
        if t.key == key:
            return t
    return None

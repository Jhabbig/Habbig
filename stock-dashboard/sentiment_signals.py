"""
sentiment_signals.py - News sentiment and calendar-based signals for stock prediction.

Provides earnings awareness, FOMC/economic calendar signals, headline sentiment,
analyst recommendations, and short interest data using yfinance.
"""

import time
import logging
from datetime import datetime, timezone, timedelta
from functools import wraps
from typing import Dict, Any

import numpy as np
import yfinance as yf

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level cache with 30-minute TTL
# ---------------------------------------------------------------------------
_cache: Dict[str, Dict[str, Any]] = {}
_CACHE_TTL = 1800  # 30 minutes in seconds


def _cache_key(func_name: str, *args) -> str:
    return f"{func_name}:{'|'.join(str(a) for a in args)}"


def _get_cached(key: str):
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < _CACHE_TTL:
        return entry["value"]
    return None


def _set_cached(key: str, value):
    _cache[key] = {"value": value, "ts": time.time()}


def cached(func):
    """Decorator that adds 30-minute TTL caching based on function name + args."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        key = _cache_key(func.__name__, *args)
        hit = _get_cached(key)
        if hit is not None:
            return hit
        result = func(*args, **kwargs)
        _set_cached(key, result)
        return result
    return wrapper


# ---------------------------------------------------------------------------
# Sentiment word lists
# ---------------------------------------------------------------------------
POSITIVE_WORDS = {
    "upgrade", "beat", "exceed", "surge", "rally", "record", "growth",
    "strong", "bullish", "buy", "raise", "positive", "outperform",
    "breakthrough", "soar", "gain", "profit", "boom", "optimistic",
}

NEGATIVE_WORDS = {
    "downgrade", "miss", "decline", "plunge", "crash", "weak", "bearish",
    "sell", "cut", "negative", "underperform", "layoff", "recall",
    "investigation", "lawsuit", "fine", "loss", "slump", "warning",
    "pessimistic", "fraud",
}

# ---------------------------------------------------------------------------
# 2026 FOMC meeting dates (each entry is the second day of the two-day meeting)
# ---------------------------------------------------------------------------
FOMC_DATES_2026 = [
    datetime(2026, 1, 28), datetime(2026, 1, 29),
    datetime(2026, 3, 17), datetime(2026, 3, 18),
    datetime(2026, 5, 5),  datetime(2026, 5, 6),
    datetime(2026, 6, 16), datetime(2026, 6, 17),
    datetime(2026, 7, 28), datetime(2026, 7, 29),
    datetime(2026, 9, 15), datetime(2026, 9, 16),
    datetime(2026, 10, 27), datetime(2026, 10, 28),
    datetime(2026, 12, 8), datetime(2026, 12, 9),
]


# ---------------------------------------------------------------------------
# 1. Earnings signal
# ---------------------------------------------------------------------------
@cached
def get_earnings_signal(ticker_yf: str) -> dict:
    """Check upcoming earnings date and return calendar-based signals."""
    defaults = {
        "days_to_earnings": 999,
        "is_earnings_week": False,
        "is_earnings_day": False,
        "pre_earnings_drift": 0.0,
    }
    try:
        ticker = yf.Ticker(ticker_yf)
        cal = ticker.calendar
        if cal is None or (hasattr(cal, "empty") and cal.empty):
            return defaults

        # yfinance .calendar can return a dict or a DataFrame depending on version
        earnings_date = None
        if isinstance(cal, dict):
            # Try common keys
            for key in ("Earnings Date", "earningsDate", "Earnings Average"):
                val = cal.get(key)
                if val is not None:
                    if isinstance(val, list) and len(val) > 0:
                        earnings_date = val[0]
                    elif isinstance(val, datetime):
                        earnings_date = val
                    break
        else:
            # DataFrame-style
            try:
                if "Earnings Date" in cal.index:
                    val = cal.loc["Earnings Date"]
                    if hasattr(val, "iloc"):
                        earnings_date = val.iloc[0]
                    else:
                        earnings_date = val
            except Exception:
                pass

        if earnings_date is None:
            return defaults

        # Normalise to date
        if hasattr(earnings_date, "date"):
            earnings_dt = earnings_date.date()
        elif isinstance(earnings_date, str):
            earnings_dt = datetime.strptime(earnings_date[:10], "%Y-%m-%d").date()
        else:
            earnings_dt = earnings_date

        today = datetime.now(timezone.utc).date()
        days_to = (earnings_dt - today).days

        # Pre-earnings drift signal: strongest in last 2 weeks before earnings
        if 0 < days_to <= 14:
            pre_drift = max(0.0, (14 - days_to) / 14.0)  # 0..1, peaks on earnings day
        elif days_to == 0:
            pre_drift = 1.0
        else:
            pre_drift = 0.0

        return {
            "days_to_earnings": days_to,
            "is_earnings_week": 0 <= days_to <= 5,
            "is_earnings_day": days_to == 0,
            "pre_earnings_drift": round(pre_drift, 4),
        }

    except Exception as e:
        logger.debug(f"get_earnings_signal({ticker_yf}): {e}")
        return defaults


# ---------------------------------------------------------------------------
# 2. Fed / economic calendar signal
# ---------------------------------------------------------------------------
@cached
def get_fed_calendar_signal() -> dict:
    """Return FOMC and macro-economic calendar signals for today."""
    defaults = {
        "days_to_fomc": 999,
        "is_fomc_day": False,
        "is_fomc_week": False,
        "is_jobs_friday": False,
        "is_likely_cpi_week": False,
    }
    try:
        today = datetime.now(timezone.utc).date()

        # Days to next FOMC
        future_fomc = [d.date() for d in FOMC_DATES_2026 if d.date() >= today]
        if future_fomc:
            next_fomc = min(future_fomc)
            days_to_fomc = (next_fomc - today).days
        else:
            days_to_fomc = 999

        is_fomc_day = any(d.date() == today for d in FOMC_DATES_2026)

        # FOMC week: today is within 0-4 calendar days before an FOMC date
        is_fomc_week = False
        for d in FOMC_DATES_2026:
            diff = (d.date() - today).days
            if 0 <= diff <= 4:
                is_fomc_week = True
                break

        # Jobs Friday: first Friday of the month
        first_day = today.replace(day=1)
        first_friday_offset = (4 - first_day.weekday()) % 7  # 4 = Friday
        first_friday = first_day + timedelta(days=first_friday_offset)
        is_jobs_friday = (today == first_friday and today.weekday() == 4)

        # CPI week: usually released 10th-15th of the month (Tue or Wed)
        is_likely_cpi_week = (10 <= today.day <= 15)

        return {
            "days_to_fomc": days_to_fomc,
            "is_fomc_day": is_fomc_day,
            "is_fomc_week": is_fomc_week,
            "is_jobs_friday": is_jobs_friday,
            "is_likely_cpi_week": is_likely_cpi_week,
        }

    except Exception as e:
        logger.debug(f"get_fed_calendar_signal(): {e}")
        return defaults


# ---------------------------------------------------------------------------
# 3. News headline sentiment
# ---------------------------------------------------------------------------
def _score_headline(headline: str) -> float:
    """Score a single headline using keyword matching. Returns -1 to 1."""
    words = set(headline.lower().split())
    pos = len(words & POSITIVE_WORDS)
    neg = len(words & NEGATIVE_WORDS)
    total = pos + neg
    if total == 0:
        return 0.0
    return (pos - neg) / total


@cached
def get_news_sentiment(ticker_yf: str) -> dict:
    """Analyse recent yfinance news headlines for sentiment."""
    defaults = {
        "sentiment_score": 0.0,
        "num_articles": 0,
        "positive_count": 0,
        "negative_count": 0,
        "headline_momentum": 0.0,
    }
    try:
        ticker = yf.Ticker(ticker_yf)
        news = ticker.news
        if not news:
            return defaults

        headlines = []
        for item in news:
            title = item.get("title") or item.get("headline") or ""
            if title:
                headlines.append(title)

        if not headlines:
            return defaults

        scores = [_score_headline(h) for h in headlines]
        positive_count = sum(1 for s in scores if s > 0)
        negative_count = sum(1 for s in scores if s < 0)
        avg_score = float(np.mean(scores))

        # Headline momentum: compare first half (newer) vs second half (older)
        mid = max(1, len(scores) // 2)
        newer = scores[:mid]
        older = scores[mid:]
        if older:
            momentum = float(np.mean(newer)) - float(np.mean(older))
        else:
            momentum = 0.0

        return {
            "sentiment_score": round(np.clip(avg_score, -1.0, 1.0), 4),
            "num_articles": len(headlines),
            "positive_count": positive_count,
            "negative_count": negative_count,
            "headline_momentum": round(np.clip(momentum, -1.0, 1.0), 4),
        }

    except Exception as e:
        logger.debug(f"get_news_sentiment({ticker_yf}): {e}")
        return defaults


# ---------------------------------------------------------------------------
# 4. Analyst recommendations signal
# ---------------------------------------------------------------------------
@cached
def get_analyst_signal(ticker_yf: str) -> dict:
    """Extract analyst buy/hold/sell counts and compute a bull ratio."""
    defaults = {
        "buy_count": 0,
        "hold_count": 0,
        "sell_count": 0,
        "bull_ratio": 0.5,
    }
    try:
        ticker = yf.Ticker(ticker_yf)

        # Try recommendations_summary first, then recommendations
        recs = None
        try:
            recs = ticker.recommendations_summary
        except Exception:
            pass

        if recs is not None and hasattr(recs, "empty") and not recs.empty:
            # recommendations_summary is a DataFrame with columns like
            # strongBuy, buy, hold, sell, strongSell
            latest = recs.iloc[0] if len(recs) > 0 else None
            if latest is not None:
                buy_count = int(latest.get("strongBuy", 0) or 0) + int(latest.get("buy", 0) or 0)
                hold_count = int(latest.get("hold", 0) or 0)
                sell_count = int(latest.get("sell", 0) or 0) + int(latest.get("strongSell", 0) or 0)
                total = buy_count + hold_count + sell_count
                bull_ratio = buy_count / total if total > 0 else 0.5
                return {
                    "buy_count": buy_count,
                    "hold_count": hold_count,
                    "sell_count": sell_count,
                    "bull_ratio": round(bull_ratio, 4),
                }

        # Fallback: .recommendations (older yfinance versions)
        recs_df = None
        try:
            recs_df = ticker.recommendations
        except Exception:
            pass

        if recs_df is not None and hasattr(recs_df, "empty") and not recs_df.empty:
            # Take last 3 months of data
            recent = recs_df.tail(30)
            if "To Grade" in recent.columns:
                grades = recent["To Grade"].str.lower()
            elif "toGrade" in recent.columns:
                grades = recent["toGrade"].str.lower()
            else:
                return defaults

            buy_terms = {"buy", "strong buy", "overweight", "outperform", "positive"}
            sell_terms = {"sell", "strong sell", "underweight", "underperform", "negative"}

            buy_count = sum(1 for g in grades if any(t in str(g) for t in buy_terms))
            sell_count = sum(1 for g in grades if any(t in str(g) for t in sell_terms))
            hold_count = len(grades) - buy_count - sell_count
            total = buy_count + hold_count + sell_count
            bull_ratio = buy_count / total if total > 0 else 0.5

            return {
                "buy_count": buy_count,
                "hold_count": hold_count,
                "sell_count": sell_count,
                "bull_ratio": round(bull_ratio, 4),
            }

        return defaults

    except Exception as e:
        logger.debug(f"get_analyst_signal({ticker_yf}): {e}")
        return defaults


# ---------------------------------------------------------------------------
# 5. Short interest signal
# ---------------------------------------------------------------------------
@cached
def get_short_interest_signal(ticker_yf: str) -> dict:
    """Return short interest metrics from yfinance info."""
    defaults = {
        "short_ratio": 0.0,
        "short_pct_float": 0.0,
        "is_heavily_shorted": False,
    }
    try:
        ticker = yf.Ticker(ticker_yf)
        info = ticker.info or {}

        short_ratio = float(info.get("shortRatio", 0) or 0)
        short_pct = float(info.get("shortPercentOfFloat", 0) or 0)

        # yfinance sometimes returns as decimal (0.05) or percent (5.0)
        # Normalise to percentage
        if 0 < short_pct < 1:
            short_pct *= 100.0

        return {
            "short_ratio": round(short_ratio, 2),
            "short_pct_float": round(short_pct, 2),
            "is_heavily_shorted": short_pct > 10.0,
        }

    except Exception as e:
        logger.debug(f"get_short_interest_signal({ticker_yf}): {e}")
        return defaults


# ---------------------------------------------------------------------------
# 6. Master function: build_sentiment_features
# ---------------------------------------------------------------------------
def build_sentiment_features(ticker_key: str, ticker_yf: str) -> dict:
    """
    Combine all sentiment/calendar signals into a flat feature dictionary.

    Args:
        ticker_key: Human-readable key (e.g. "AAPL") used as a prefix.
        ticker_yf: yfinance-compatible ticker symbol.

    Returns:
        Flat dict with prefixed feature names.
    """
    features: Dict[str, Any] = {}

    # Earnings
    earnings = get_earnings_signal(ticker_yf)
    for k, v in earnings.items():
        features[f"earn_{k}"] = float(v) if isinstance(v, (bool, int, float, np.floating)) else v

    # Fed calendar
    fed = get_fed_calendar_signal()
    for k, v in fed.items():
        features[f"fed_{k}"] = float(v) if isinstance(v, (bool, int, float, np.floating)) else v

    # News sentiment
    news = get_news_sentiment(ticker_yf)
    for k, v in news.items():
        features[f"news_{k}"] = float(v) if isinstance(v, (bool, int, float, np.floating)) else v

    # Analyst signal
    analyst = get_analyst_signal(ticker_yf)
    for k, v in analyst.items():
        features[f"analyst_{k}"] = float(v) if isinstance(v, (bool, int, float, np.floating)) else v

    # Short interest
    short = get_short_interest_signal(ticker_yf)
    for k, v in short.items():
        features[f"short_{k}"] = float(v) if isinstance(v, (bool, int, float, np.floating)) else v

    # Convert booleans to 0/1 for ML consumption
    for k, v in features.items():
        if isinstance(v, bool):
            features[k] = 1.0 if v else 0.0

    return features


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    test_ticker = "AAPL"

    print("=" * 60)
    print(f"  Sentiment Signals Test  --  {test_ticker}")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    print("\n--- Earnings Signal ---")
    earnings = get_earnings_signal(test_ticker)
    for k, v in earnings.items():
        print(f"  {k}: {v}")

    print("\n--- Fed Calendar Signal ---")
    fed = get_fed_calendar_signal()
    for k, v in fed.items():
        print(f"  {k}: {v}")

    print("\n--- News Sentiment ---")
    news = get_news_sentiment(test_ticker)
    for k, v in news.items():
        print(f"  {k}: {v}")

    print("\n--- Analyst Signal ---")
    analyst = get_analyst_signal(test_ticker)
    for k, v in analyst.items():
        print(f"  {k}: {v}")

    print("\n--- Short Interest Signal ---")
    short = get_short_interest_signal(test_ticker)
    for k, v in short.items():
        print(f"  {k}: {v}")

    print("\n--- Combined Features (build_sentiment_features) ---")
    all_features = build_sentiment_features("AAPL", test_ticker)
    for k, v in sorted(all_features.items()):
        print(f"  {k}: {v}")

    print("\n" + "=" * 60)
    print("  Done.")
    print("=" * 60)

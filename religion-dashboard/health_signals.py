"""Pope health-signal aggregator.

Scans a list of news items (title + summary) for language indicating
the Pope's physical state — hospitalisations, cancelled audiences,
missed appearances, illness, recovery. Each phrase has a severity
weight (0-3); we aggregate over a rolling 14-day window into a single
health-risk score in [0, 10].

WHY THIS MATTERS: a 1-2 week window of "Pope cancels audience" / "rests"
language has historically preceded papal death events by months to
weeks. For Polymarket "Pope alive on date X" markets, this is the
single highest-signal input beyond age alone.

THIS IS LEXICAL, NOT MEDICAL. We're not diagnosing — we're tracking the
*reporting* of the Pope's state, which is what the market reacts to.
False positives (e.g., scheduled rest) are expected and acceptable;
the signal is most useful in aggregate.

OUTPUT
  score          — 0-10 composite. <2 = quiet. 2-5 = elevated. 5+ = high.
  recent_signals — list of {date, source, title, phrase, weight}
  windows_days   — number of days the score sums over (default 14)
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional


# Each phrase has a severity weight. Higher = more serious.
# Lower-case regex patterns, applied to the combined title + summary.
HEALTH_PHRASES: list[tuple[str, int, str]] = [
    # ─ weight 3: hospitalisation / serious medical ─
    (r"\bpope\b[^.]{0,40}\bhospitali[sz]ed\b",        3, "Pope hospitalised"),
    (r"\bpope\b[^.]{0,40}\bsurgery\b",                3, "Pope surgery"),
    (r"\bpope\b[^.]{0,40}\bintubat",                  3, "Pope intubated"),
    (r"\bpope\b[^.]{0,40}\bcritical condition\b",     3, "Pope critical condition"),
    (r"\bpope\b[^.]{0,40}\bicu\b",                    3, "Pope ICU"),
    (r"\bpope\b[^.]{0,40}\bpneumonia\b",              3, "Pope pneumonia"),
    (r"\bpope\b[^.]{0,40}\binfection\b",              3, "Pope infection"),
    (r"\bpope\b[^.]{0,40}\bdouble pneumonia\b",       3, "Pope double pneumonia"),
    # ─ weight 2: cancellations / missed events ─
    (r"\bpope\b[^.]{0,40}\bcancel(s|led|s|ling)?\b",  2, "Pope cancels"),
    (r"\bpope\b[^.]{0,40}\bunable to\b",              2, "Pope unable to"),
    (r"\bpope\b[^.]{0,40}\babsent\b",                 2, "Pope absent"),
    (r"\bpope\b[^.]{0,40}\bskipp",                    2, "Pope skips"),
    (r"\bpope\b[^.]{0,40}\bmissed\b",                 2, "Pope missed"),
    (r"\bpope\b[^.]{0,40}\bbronchitis\b",             2, "Pope bronchitis"),
    (r"\bpope\b[^.]{0,40}\bflu\b",                    2, "Pope flu"),
    (r"\bpope\b[^.]{0,40}\bcold\b",                   2, "Pope cold"),
    (r"\bgeneral audience\b[^.]{0,40}\bcancel",       2, "general audience cancelled"),
    (r"\bangelus\b[^.]{0,40}\bcancel",                2, "angelus cancelled"),
    # ─ weight 1: rest / mild ─
    (r"\bpope\b[^.]{0,40}\brest(s|ing)?\b",           1, "Pope rests"),
    (r"\bpope\b[^.]{0,40}\bfatigue\b",                1, "Pope fatigue"),
    (r"\bpope\b[^.]{0,40}\btired\b",                  1, "Pope tired"),
    (r"\bpope\b[^.]{0,40}\brecovery\b",               1, "Pope recovery"),
    (r"\bpope\b[^.]{0,40}\bdoctor",                   1, "Pope doctor"),
    (r"\bpope\b[^.]{0,40}\bhealth\b",                 1, "Pope health"),
    (r"\bpope\b[^.]{0,40}\bbreathing\b",              1, "Pope breathing"),
    (r"\bpope\b[^.]{0,40}\bknee\b",                   1, "Pope knee"),
    (r"\bpope\b[^.]{0,40}\bwheelchair\b",             1, "Pope wheelchair"),
]

# Compile once.
_COMPILED = [(re.compile(pat, re.IGNORECASE), weight, label)
             for pat, weight, label in HEALTH_PHRASES]


def _parse_news_date(s: str) -> Optional[date]:
    """News RSS items come with varied date formats. Best-effort."""
    if not s:
        return None
    s = s.strip()
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def compute_health_signal(news_items: list[dict], today: Optional[date] = None,
                          window_days: int = 14) -> dict:
    """Score the recent Pope health-signal volume.

    Args:
        news_items: each item should have 'title', 'summary', 'source',
                    and a 'published' string (any common RSS format).
        today:      reference date (defaults to today)
        window_days: how far back to sum signals (default 14)

    Returns:
        {
            "score": float in [0, 10],
            "band": "quiet" | "elevated" | "high" | "critical",
            "windows_days": int,
            "match_count": int,
            "recent_signals": list of matched signals (newest first)
        }
    """
    if today is None:
        today = date.today()
    cutoff = today - timedelta(days=window_days)

    signals: list[dict] = []
    sum_weight = 0
    for item in news_items:
        text = ((item.get("title") or "") + " " + (item.get("summary") or "")).lower()
        if "pope" not in text and "vatican" not in text and "francis" not in text:
            # Quick rejection — saves regex work
            continue
        # Date filter
        pub = _parse_news_date(item.get("published") or "")
        if pub and pub < cutoff:
            continue
        if pub and pub > today + timedelta(days=1):
            # Future-dated → suspicious, skip
            continue
        # Run regex matches
        matched_here = False
        for pattern, weight, label in _COMPILED:
            if pattern.search(text):
                signals.append({
                    "date": (pub or today).isoformat(),
                    "source": item.get("source", ""),
                    "title": item.get("title", ""),
                    "link": item.get("link", ""),
                    "phrase": label,
                    "weight": weight,
                })
                sum_weight += weight
                matched_here = True
                break  # one match per article — don't double-count

    # Normalise to 0-10. Calibration: 0 articles = 0; ~6 weight-1 articles
    # in a 14-day window = 4 (elevated); 1 hospitalisation alone = 3;
    # 3+ severe articles = approaches 10.
    score = min(10.0, sum_weight * 0.8)

    if score < 2:
        band = "quiet"
    elif score < 5:
        band = "elevated"
    elif score < 8:
        band = "high"
    else:
        band = "critical"

    signals.sort(key=lambda s: s["date"], reverse=True)

    return {
        "score": round(score, 2),
        "band": band,
        "window_days": window_days,
        "match_count": len(signals),
        "sum_weight": sum_weight,
        "recent_signals": signals[:20],  # cap output
    }

"""US presidential elections + mood-index backtest.

Election outcomes are hard-coded — public historical record, not data
the dashboard should fetch. We carry the popular-vote share for the
incumbent president's party and a boolean for whether the incumbent
party won the presidency (electoral outcome). 2000 and 2016 are flagged
because the popular-vote winner lost — the boolean follows the EC.

Backtest:
  For every election we read the mood-index value at four horizons
  before election day (12mo, 6mo, 3mo, 1mo). At each horizon we ask:
    (a) does mood ≥ 50 correctly predict incumbent_won?
    (b) what's the linear correlation between mood and
        incumbent_pop_vote_pct?

We deliberately report N small. n=11 elections since 1980 — these are
the elections where the production mood index has enough underlying
data (UMCSENT monthly from 1978) to be honest. We do NOT include 1976
and earlier; the composite has too many missing components for an
apples-to-apples comparison.
"""

from __future__ import annotations

from . import historical_mood


# (year, election ISO date, incumbent party, incumbent_won (EC), incumbent_pop_vote_pct,
#  note for the "asterisk" cases)
ELECTIONS: list[dict] = [
    {"year": 1980, "date": "1980-11-04", "incumbent_party": "D", "incumbent_won": False, "incumbent_pop_vote_pct": 41.0, "note": "Carter loses to Reagan"},
    {"year": 1984, "date": "1984-11-06", "incumbent_party": "R", "incumbent_won": True,  "incumbent_pop_vote_pct": 58.8, "note": "Reagan re-elected"},
    {"year": 1988, "date": "1988-11-08", "incumbent_party": "R", "incumbent_won": True,  "incumbent_pop_vote_pct": 53.4, "note": "Bush Sr wins"},
    {"year": 1992, "date": "1992-11-03", "incumbent_party": "R", "incumbent_won": False, "incumbent_pop_vote_pct": 37.5, "note": "Bush Sr loses to Clinton"},
    {"year": 1996, "date": "1996-11-05", "incumbent_party": "D", "incumbent_won": True,  "incumbent_pop_vote_pct": 49.2, "note": "Clinton re-elected"},
    {"year": 2000, "date": "2000-11-07", "incumbent_party": "D", "incumbent_won": False, "incumbent_pop_vote_pct": 48.4, "note": "Gore wins popular, loses EC"},
    {"year": 2004, "date": "2004-11-02", "incumbent_party": "R", "incumbent_won": True,  "incumbent_pop_vote_pct": 50.7, "note": "Bush Jr re-elected"},
    {"year": 2008, "date": "2008-11-04", "incumbent_party": "R", "incumbent_won": False, "incumbent_pop_vote_pct": 45.7, "note": "McCain loses to Obama"},
    {"year": 2012, "date": "2012-11-06", "incumbent_party": "D", "incumbent_won": True,  "incumbent_pop_vote_pct": 51.1, "note": "Obama re-elected"},
    {"year": 2016, "date": "2016-11-08", "incumbent_party": "D", "incumbent_won": False, "incumbent_pop_vote_pct": 48.2, "note": "Clinton wins popular, loses EC"},
    {"year": 2020, "date": "2020-11-03", "incumbent_party": "R", "incumbent_won": False, "incumbent_pop_vote_pct": 46.8, "note": "Trump loses to Biden"},
    {"year": 2024, "date": "2024-11-05", "incumbent_party": "D", "incumbent_won": False, "incumbent_pop_vote_pct": 48.3, "note": "Harris loses to Trump"},
]

HORIZONS_MONTHS = [12, 6, 3, 1]
THRESHOLD = 50.0  # mood ≥ 50 → predict incumbent_won


def _shift_months(iso_date: str, months_back: int) -> str:
    """Subtract `months_back` whole months from an ISO date. Day is set
    to 01 because we're matching against monthly readings anyway."""
    y = int(iso_date[:4])
    m = int(iso_date[5:7])
    total = y * 12 + (m - 1) - months_back
    y2, m2 = divmod(total, 12)
    return f"{y2:04d}-{m2 + 1:02d}-01"


def _lookup_mood(history: list[dict], as_of_month: str) -> float | None:
    """Pick the latest mood value with date ≤ `as_of_month`."""
    best: float | None = None
    for row in history:
        if row["date"] > as_of_month:
            break
        if row.get("overall") is not None:
            best = row["overall"]
    return best


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def run(rows: list[dict]) -> dict:
    """Compute the full backtest payload from raw FRED rows."""
    history = historical_mood.monthly_history(rows, start="1978-01-01")

    calls: list[dict] = []
    for elx in ELECTIONS:
        per_horizon: dict[str, dict] = {}
        for hmo in HORIZONS_MONTHS:
            as_of = _shift_months(elx["date"], hmo)
            mood = _lookup_mood(history, as_of)
            predicted = None
            correct = None
            if mood is not None:
                predicted = mood >= THRESHOLD
                correct = bool(predicted) == bool(elx["incumbent_won"])
            per_horizon[f"h{hmo}mo"] = {
                "as_of": as_of,
                "mood": mood,
                "predicted_incumbent_won": predicted,
                "correct": correct,
            }
        calls.append({
            **elx,
            "horizons": per_horizon,
        })

    # Per-horizon accuracy + correlation
    summary: dict[str, dict] = {}
    for hmo in HORIZONS_MONTHS:
        key = f"h{hmo}mo"
        moods = []
        votes = []
        right = 0
        total = 0
        for c in calls:
            row = c["horizons"][key]
            if row["mood"] is None:
                continue
            total += 1
            if row["correct"]:
                right += 1
            moods.append(row["mood"])
            votes.append(c["incumbent_pop_vote_pct"])
        summary[key] = {
            "n": total,
            "correct": right,
            "accuracy_pct": (right / total * 100.0) if total else None,
            "correlation_with_pop_vote": _pearson(moods, votes),
        }

    # Headline (best-performing horizon by accuracy)
    headline = None
    best_acc = -1.0
    for hmo in HORIZONS_MONTHS:
        s = summary[f"h{hmo}mo"]
        if s["accuracy_pct"] is not None and s["accuracy_pct"] > best_acc:
            best_acc = s["accuracy_pct"]
            headline = {
                "horizon_months": hmo,
                "correct": s["correct"],
                "n": s["n"],
                "accuracy_pct": s["accuracy_pct"],
            }

    return {
        "elections": calls,
        "horizons_months": HORIZONS_MONTHS,
        "threshold": THRESHOLD,
        "by_horizon": summary,
        "headline": headline,
        "history": history,
    }

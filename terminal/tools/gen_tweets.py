#!/usr/bin/env python3.11
"""Generate SYNTHETIC GOP tweets for narve Terminal stress/demo imports.

Usage:
    python3.11 tools/gen_tweets.py [out_dir] [n_tweets] [n_accounts] [seed]
    # defaults:            .          25000      1200        42

Deterministic for a given (n_tweets, n_accounts, seed). Writes:
    out_dir/tweets_<n>.csv        — messages-ingest CSV (drag into INGEST)
    out_dir/gop_resolutions.csv   — resolutions for the 8 synthetic questions

Design: each account has a latent skill (P(stance points the right way)) and a
Pareto-drawn posting volume, so tweet counts per user follow a heavy power law
(a few accounts post hundreds, most a handful). A planted cohort (first 30
accounts) is genuinely skilled — after resolution, credibility scoring should
separate them from the noise. ~40% of tweets carry an explicit stance; the
rest are context-only.
"""
import csv
import random
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

OUT = sys.argv[1] if len(sys.argv) > 1 else "."
N_TWEETS = int(sys.argv[2]) if len(sys.argv) > 2 else 25_000
N_ACCOUNTS = int(sys.argv[3]) if len(sys.argv) > 3 else 1_200
SEED = int(sys.argv[4]) if len(sys.argv) > 4 else 42
random.seed(SEED)

RESOLVED_QS = [
    ("gop-primary-oh-sen-2026", "yes"), ("gop-primary-az-gov-2026", "no"),
    ("gop-speaker-vote-jul-2026", "yes"), ("gop-platform-vote-2026", "yes"),
    ("gop-debate-happens-aug-2026", "no"), ("gop-ga-runoff-2026", "yes"),
    ("gop-tx-convention-2026", "no"), ("gop-nh-endorsement-2026", "yes"),
]
LIVE_QS = [
    "midterm-house-gop-2026", "midterm-senate-dem-2026", "midterm-tx-sen-gop-2026",
    "midterm-oh-sen-gop-2026", "midterm-mi-sen-dem-2026", "midterm-az-sen-dem-2026",
    "midterm-ga-sen-gop-2026", "midterm-nv-sen-gop-2026", "midterm-trifecta-gop-2026",
    "midterm-pa-gov-dem-2026", "midterm-wi-sen-dem-2026", "midterm-house-popvote-dem-2026",
]
LIVE_CONSENSUS = {q: random.uniform(0.35, 0.7) for q in LIVE_QS}

FIRST = ["mike", "beth", "carl", "dana", "eli", "fran", "gus", "hana", "ivan",
         "jade", "kyle", "lena", "moe", "nina", "otis", "page", "quinn", "rosa",
         "seth", "tara", "umar", "vera", "walt", "xena", "yosef", "zara"]
STYLE = ["maga", "bluewave", "polls", "capitol", "beltway", "swing", "gop",
         "dem", "midterm", "precinct", "county", "ticket"]

TMPL_STANCE = [
    "Calling it now: {q} — I'd put it around {pct}%.",
    "My read after tonight's numbers: {q} sits near {pct}%.",
    "Been tracking this all week. {q}? Roughly {pct}% imo.",
    "Hot take but data-backed: {q} at {pct}%.",
    "Updated my model, {q} now {pct}% from where I sit.",
    "Quietly repositioning: {q} feels like {pct}% after today.",
]
TMPL_CTX = [
    "Turnout chatter in {st} is wild today. GOP field offices packed.",
    "New fundraising numbers dropping tomorrow for the {st} race.",
    "Canvassed {st} this weekend — mood is tense on both sides.",
    "GOP county chairs in {st} are quietly nervous, per two people I spoke to.",
    "Ad spend just tripled in {st}. Somebody's internal polling moved.",
    "Early-vote requests in {st} running ahead of 2022 pace.",
]
STATES = ["OH", "AZ", "GA", "TX", "NH", "MI", "NV", "PA", "WI", "NC"]

handles, skills, weights = [], [], []
for i in range(N_ACCOUNTS):
    handles.append(f"{random.choice(STYLE)}_{random.choice(FIRST)}_{i:05d}")
    skill = random.betavariate(5, 5)
    if i < 30:
        skill = random.uniform(0.78, 0.92)   # the planted genuinely-good cohort
    skills.append(skill)
    weights.append(random.paretovariate(1.3))  # heavy tail: volumes vary wildly

anchor = datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc)
SPAN_S = 45 * 86_400   # 45 days, second resolution -> dedupe-safe at 100k+
picks = random.choices(range(N_ACCOUNTS), weights=weights, k=N_TWEETS)

rows = []
for idx in picks:
    handle, skill = handles[idx], skills[idx]
    sent = (anchor - timedelta(seconds=random.randint(1, SPAN_S))
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
    if random.random() < 0.4:
        if random.random() < 0.55:
            qid, outcome = random.choice(RESOLVED_QS)
            right = random.random() < skill
            hi = (outcome == "yes") == right
            p = random.uniform(0.55, 0.95) if hi else random.uniform(0.05, 0.45)
        else:
            qid = random.choice(LIVE_QS)
            p = min(0.95, max(0.05, random.gauss(LIVE_CONSENSUS[qid], 0.12)))
        text = random.choice(TMPL_STANCE).format(q=qid, pct=round(p * 100))
        rows.append((f"x:{handle}", text, sent, qid, f"{p:.2f}"))
    else:
        text = random.choice(TMPL_CTX).format(st=random.choice(STATES))
        qid = random.choice(LIVE_QS + [q for q, _ in RESOLVED_QS]) \
            if random.random() < 0.5 else ""
        rows.append((f"x:{handle}", text, sent, qid, ""))

out_csv = f"{OUT}/tweets_{N_TWEETS}.csv"
with open(out_csv, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["source_id", "text", "sent_at", "question_id", "stance"])
    w.writerows(rows)
with open(f"{OUT}/gop_resolutions.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["question_id", "outcome", "resolved_at"])
    for qid, outcome in RESOLVED_QS:
        w.writerow([qid, outcome, "2026-08-17T22:00:00Z"])

per_user = Counter(r[0] for r in rows)
counts = sorted(per_user.values())
stance_n = sum(1 for r in rows if r[4])
print(f"wrote {out_csv}: {len(rows)} tweets ({stance_n} with stance) "
      f"from {len(per_user)} active accounts (of {N_ACCOUNTS})")
print(f"tweets/user: max={counts[-1]} p90={counts[int(len(counts)*0.9)]} "
      f"median={counts[len(counts)//2]} min={counts[0]}")

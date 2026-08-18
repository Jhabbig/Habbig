#!/usr/bin/env python3.11
"""Generate 25k SYNTHETIC GOP tweets for a narve Terminal stress test.

Deterministic (seed=42). 1,200 fake accounts, each with a latent skill
(probability their stance points the right way). 8 already-decided synthetic
GOP questions (outcomes predetermined here) + the 12 live sample midterm
questions as targets. ~40% of tweets carry an explicit stance; the rest are
context-only. The point: after resolution, credibility should SEPARATE the
skilled accounts from the noise.
"""
import csv
import random
import sys
from datetime import datetime, timedelta, timezone

random.seed(42)
OUT = sys.argv[1] if len(sys.argv) > 1 else "."
N_TWEETS = 25_000
N_ACCOUNTS = 1_200

# Synthetic resolved cohort: (question_id, outcome yes|no)
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
]
TMPL_CTX = [
    "Turnout chatter in {st} is wild today. GOP field offices packed.",
    "New fundraising numbers dropping tomorrow for the {st} race.",
    "Canvassed {st} this weekend — mood is tense on both sides.",
    "GOP county chairs in {st} are quietly nervous, per two people I spoke to.",
    "Ad spend just tripled in {st}. Somebody's internal polling moved.",
]
STATES = ["OH", "AZ", "GA", "TX", "NH", "MI", "NV", "PA", "WI", "NC"]

accounts = []
for i in range(N_ACCOUNTS):
    handle = f"{random.choice(STYLE)}_{random.choice(FIRST)}_{i:04d}"
    skill = random.betavariate(5, 5)          # most ~0.5, tails both ways
    if i < 30:
        skill = random.uniform(0.78, 0.92)    # a small genuinely-good cohort
    weight = random.paretovariate(1.6)        # power-law tweet volume
    accounts.append((handle, skill, weight))
total_w = sum(a[2] for a in accounts)

anchor = datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc)
rows = []
for n in range(N_TWEETS):
    r = random.random() * total_w
    acc = None
    for handle, skill, w in accounts:
        r -= w
        if r <= 0:
            acc = (handle, skill)
            break
    if acc is None:
        acc = (accounts[-1][0], accounts[-1][1])
    handle, skill = acc
    sent = (anchor - timedelta(minutes=random.randint(1, 60 * 24 * 45))
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
    if random.random() < 0.4:                 # stance tweet
        if random.random() < 0.55:            # on a resolved question
            qid, outcome = random.choice(RESOLVED_QS)
            right = random.random() < skill
            hi = (outcome == "yes") == right
            p = random.uniform(0.55, 0.95) if hi else random.uniform(0.05, 0.45)
        else:                                 # on a live question
            qid = random.choice(LIVE_QS)
            p = min(0.95, max(0.05, random.gauss(LIVE_CONSENSUS[qid], 0.12)))
        text = random.choice(TMPL_STANCE).format(q=qid, pct=round(p * 100))
        rows.append((f"x:{handle}", text, sent, qid, f"{p:.2f}"))
    else:                                     # context-only tweet
        text = random.choice(TMPL_CTX).format(st=random.choice(STATES))
        qid = random.choice(LIVE_QS + [q for q, _ in RESOLVED_QS]) \
            if random.random() < 0.5 else ""
        rows.append((f"x:{handle}", text, sent, qid, ""))

with open(f"{OUT}/tweets_25k.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["source_id", "text", "sent_at", "question_id", "stance"])
    w.writerows(rows)
with open(f"{OUT}/gop_resolutions.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["question_id", "outcome", "resolved_at"])
    for qid, outcome in RESOLVED_QS:
        w.writerow([qid, outcome, "2026-08-17T22:00:00Z"])
stance_n = sum(1 for r in rows if r[4])
print(f"wrote {len(rows)} tweets ({stance_n} with stance) from {N_ACCOUNTS} accounts")

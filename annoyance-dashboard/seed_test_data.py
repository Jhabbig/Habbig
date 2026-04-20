"""
Seed fake data so you can develop the UI before Reddit/Claude fill the DB.

Usage:
    python seed_test_data.py --posts 200 --hours 48

Populates posts, classifications, annoyance_index, entity_counts, and one
synthetic spike. Idempotent on rerun — wipes existing test rows first.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone

import config
import db

# A handful of plausible angry-post templates with named entities. Not real
# content; purely for UI dev.
TEMPLATES = [
    ("United Airlines cancelled my flight with zero notice. Third time this month.", ["United Airlines"], 85),
    ("Apple Music is broken AGAIN. Songs just skip at random.", ["Apple"], 70),
    ("Tesla autopilot almost put me into a median on the 101. Not OK.", ["Tesla"], 92),
    ("Spotify Wrapped rollout is a mess, app keeps crashing.", ["Spotify"], 60),
    ("Comcast Xfinity outage in my whole neighborhood again.", ["Comcast"], 78),
    ("Amazon delivery left my package in the rain. Fourth time this year.", ["Amazon"], 72),
    ("T-Mobile text-to-speech voicemail transcript is unreadable garbage.", ["T-Mobile"], 55),
    ("Meta just killed another feature nobody asked them to remove.", ["Meta"], 65),
    ("Google Calendar ate my meeting invites. Lost a full day of scheduling.", ["Google"], 80),
    ("Delta Airlines baggage lost AGAIN. I hate this airline.", ["Delta Airlines"], 88),
    ("Just had a great coffee. Nothing to complain about today.", [], 10),
    ("Genuinely love the new Apple keyboard, finally usable.", ["Apple"], 8),
    ("Microsoft Teams is actually fine this morning, I'm shocked.", ["Microsoft"], 20),
    ("Netflix removed my favorite show overnight. Who made this call?", ["Netflix"], 68),
    ("AT&T billing surprise — $40 'service fee' nobody mentioned.", ["AT&T"], 82),
    ("Verizon support put me on hold for 90 minutes. Unacceptable.", ["Verizon"], 90),
    ("Tesla charging station down, no ETA, no apology.", ["Tesla"], 86),
    ("United Airlines gate agent was actually fantastic today.", ["United Airlines"], 15),
    ("American Airlines overbooked again, classic.", ["American Airlines"], 77),
    ("Apple iCloud is down. Cannot access any photos.", ["Apple"], 84),
]

SENTIMENTS_BY_SCORE = [
    (80, "angry"),
    (55, "frustrated"),
    (25, "neutral"),
    (0, "positive"),
]


def score_to_sentiment(score: int) -> str:
    for threshold, label in SENTIMENTS_BY_SCORE:
        if score >= threshold:
            return label
    return "neutral"


def clear_test_rows() -> None:
    """Remove previously-seeded synthetic rows so we can rerun cleanly."""
    with db.cursor() as cur:
        cur.execute("DELETE FROM spikes WHERE entity LIKE '%_synthetic'")
        cur.execute("DELETE FROM classifications WHERE model = 'seed-fake'")
        cur.execute("DELETE FROM posts WHERE id LIKE 'seed:%'")
        cur.execute("DELETE FROM annoyance_index WHERE hour LIKE '%seed%'")
        # Keep entity_counts — aggregator rebuilds them from classifications anyway


def seed(num_posts: int, hours: int) -> None:
    db.init_db()
    clear_test_rows()

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    model = "seed-fake"

    for i in range(num_posts):
        template_idx = random.randrange(len(TEMPLATES))
        content, entities, base_score = TEMPLATES[template_idx]
        # Jitter the score so the index line isn't flat
        score = max(0, min(100, base_score + random.randint(-10, 10)))

        # Spread posts randomly across the last `hours` hours
        hours_ago = random.uniform(0, hours)
        posted_at_dt = now - timedelta(hours=hours_ago)
        posted_at = posted_at_dt.isoformat()

        post_id = f"seed:{i:05d}"
        db.insert_post(
            id=post_id,
            source="reddit",
            source_channel=f"r/{random.choice(config.REDDIT_SUBS)}",
            author=f"seed_user_{i % 20}",
            content=content,
            posted_at=posted_at,
            url=None,
            engagement=random.randint(1, 200),
            keyword=None,
        )

        # Synthetic classification
        entity_rows = [
            {
                "name": name,
                "type": "company",
                "salience": 0.9,
                "sentiment": score_to_sentiment(score),
            }
            for name in entities
        ]
        db.insert_classification(
            post_id=post_id,
            annoyance_score=float(score),
            sentiment=score_to_sentiment(score),
            primary_topic="seed_test",
            entities=entity_rows,
            model=model,
        )
        db.mark_classified(post_id, status=1)

    # Rebuild aggregates for the last N hours
    from aggregator import rebuild_hour
    for h in range(hours + 1):
        hour_dt = now - timedelta(hours=h)
        hour_iso = hour_dt.isoformat()
        rebuild_hour(hour_iso)

    # Record a synthetic spike so /api/spikes returns something visible
    db.insert_spike(
        entity="United Airlines",
        detected_hour=now.isoformat(),
        z_score=4.2,
        multiple_of_baseline=5.8,
        avg_annoyance=84.0,
        count=12,
        sample_post_ids=[f"seed:{i:05d}" for i in range(3)],
        summary="Flight cancellations and poor communication flagged across multiple posts.",
    )

    # Source health row
    db.upsert_source_status("reddit", ok=True, posts_today=num_posts)

    print(f"Seeded {num_posts} posts across {hours} hours.")
    print("Boot the server and open http://localhost:8053/")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--posts", type=int, default=200)
    parser.add_argument("--hours", type=int, default=48)
    args = parser.parse_args()
    try:
        seed(args.posts, args.hours)
    except Exception as e:
        print(f"seed failed: {e}", file=sys.stderr)
        raise

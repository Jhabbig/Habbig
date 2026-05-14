"""
Tests for the Happiness view (DECISIONS.md #7 unlock, 2026-05-14).

Covers:
  1. POLARITY FILTER — /api/happiness/spikes returns only polarity='positive'.
  2. TAB ENABLED   — index.html / entity.html no longer carry `tab-disabled`.
  3. INVERTED THEME — CSS ships `.spike-card.positive` with thicker border,
                      stays monochrome (no green chroma).
  4. MIGRATION IDEMPOTENT — running init_db()+migrations.run_all() any
                            number of times doesn't error.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import db


pytestmark = pytest.mark.integration


_REPO = Path(__file__).resolve().parents[1]
_STATIC = _REPO / "static"


def _hour_iso(offset: int = 0) -> str:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return (now - timedelta(hours=offset)).isoformat()


def _insert_positive_classification(post_id: str, entity_name: str = "Acme") -> None:
    db.insert_post(
        id=post_id, source="reddit",
        content=f"{entity_name} just hit a milestone — incredible recovery",
        posted_at=_hour_iso(0),
    )
    db.insert_classification(
        post_id=post_id, annoyance_score=55.0, sentiment="positive",
        primary_topic="business",
        entities=[{
            "name": entity_name, "type": "company",
            "salience": 0.9, "sentiment": "positive",
        }],
        model="claude-sonnet-test",
    )


# ── 1. Polarity filter ───────────────────────────────────────────────────────

def test_happiness_endpoint_returns_only_positive_polarity_rows(fresh_db):
    """Mixed-polarity DB → only positive rows appear in the happiness view."""
    import migrations
    migrations.run_all()

    db.insert_spike(
        entity="BadCo", detected_hour=_hour_iso(0),
        z_score=4.5, multiple_of_baseline=5.0, avg_annoyance=80.0,
        count=12, sample_post_ids=[], polarity="negative",
    )
    db.insert_spike(
        entity="OtherBad", detected_hour=_hour_iso(1),
        z_score=3.2, multiple_of_baseline=3.5, avg_annoyance=72.0,
        count=8, sample_post_ids=[], polarity="negative",
    )
    db.insert_spike(
        entity="HappyCo", detected_hour=_hour_iso(2),
        z_score=3.8, multiple_of_baseline=4.2, avg_annoyance=55.0,
        count=10, sample_post_ids=[], polarity="positive",
    )

    import happiness
    positive = happiness.recent_happiness_spikes(limit=20)
    assert len(positive) == 1, f"expected 1 positive spike, got {len(positive)}"
    assert positive[0]["entity"] == "HappyCo"
    assert positive[0].get("polarity") == "positive"

    all_spikes = db.get_recent_spikes(limit=20)
    assert len(all_spikes) == 3


def test_get_recent_spikes_polarity_filter(fresh_db):
    import migrations
    migrations.run_all()

    db.insert_spike(
        entity="NegEntity", detected_hour=_hour_iso(0),
        z_score=4.0, multiple_of_baseline=4.0, avg_annoyance=70.0,
        count=10, sample_post_ids=[], polarity="negative",
    )
    db.insert_spike(
        entity="PosEntity", detected_hour=_hour_iso(1),
        z_score=3.5, multiple_of_baseline=4.5, avg_annoyance=60.0,
        count=8, sample_post_ids=[], polarity="positive",
    )

    pos = db.get_recent_spikes(limit=10, polarity="positive")
    assert len(pos) == 1 and pos[0]["entity"] == "PosEntity"

    neg = db.get_recent_spikes(limit=10, polarity="negative")
    assert len(neg) == 1 and neg[0]["entity"] == "NegEntity"

    assert len(db.get_recent_spikes(limit=10)) == 2


def test_api_happiness_spikes_returns_positive_only(test_client, paywall_env, fresh_db):
    """End-to-end through the ASGI stack."""
    from tests.conftest import pro_headers
    import migrations
    migrations.run_all()

    db.insert_spike(
        entity="ShouldNotAppear", detected_hour=_hour_iso(0),
        z_score=4.0, multiple_of_baseline=4.0, avg_annoyance=80.0,
        count=12, sample_post_ids=[], polarity="negative",
    )
    db.insert_spike(
        entity="ShouldAppear", detected_hour=_hour_iso(1),
        z_score=3.5, multiple_of_baseline=4.5, avg_annoyance=55.0,
        count=8, sample_post_ids=[], polarity="positive",
    )

    r = test_client.get("/api/happiness/spikes", headers=pro_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["polarity"] == "positive"
    names = [s["entity"] for s in body["spikes"]]
    assert "ShouldAppear" in names
    assert "ShouldNotAppear" not in names


def test_api_happiness_entities_min_mentions_threshold(test_client, paywall_env, fresh_db):
    from tests.conftest import pro_headers

    for i in range(6):
        _insert_positive_classification(f"hot-{i}", entity_name="Surfaced")
    for i in range(4):
        _insert_positive_classification(f"meh-{i}", entity_name="Filtered")

    r = test_client.get("/api/happiness/entities?limit=20", headers=pro_headers())
    assert r.status_code == 200
    names = [e["entity"] for e in r.json()["entities"]]
    assert "Surfaced" in names
    assert "Filtered" not in names


# ── 2. Tab enabled ───────────────────────────────────────────────────────────

def test_happiness_tab_no_longer_disabled_in_index_html():
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    assert "tab-disabled" not in html
    assert 'data-view="happiness"' in html
    assert 'data-view="annoyance"' in html


def test_entity_page_tab_no_longer_disabled():
    html = (_STATIC / "entity.html").read_text(encoding="utf-8")
    assert "tab-disabled" not in html
    assert "#happiness" in html
    assert "#annoyance" in html


def test_happiness_view_section_exists():
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="happiness-view"' in html
    assert 'id="happiness-spikes-list"' in html
    assert 'id="happiness-entities-list"' in html
    assert "Happiness Map" in html


# ── 3. Inverted theme (monochrome, thicker borders) ──────────────────────────

def test_css_ships_positive_spike_card_with_thicker_border():
    css = (_STATIC / "annoyance.css").read_text(encoding="utf-8")
    assert ".spike-card.positive" in css
    pattern = r"\.spike-card\.positive\s*\{([^}]+)\}"
    match = re.search(pattern, css)
    assert match
    body = match.group(1)
    assert re.search(r"border-width\s*:\s*[2-9]px", body), (
        "expected border-width >= 2px on .spike-card.positive"
    )


def test_css_stays_monochrome_no_green_chroma():
    """The happiness section must not use green chroma as a CSS VALUE.
    Comments explaining the no-green rule are allowed (and required).
    """
    css = (_STATIC / "annoyance.css").read_text(encoding="utf-8")
    # Strip ALL CSS block comments first so the explanatory comment for the
    # happiness section (which mentions "green" to explain the no-green rule)
    # doesn't trigger a false positive.
    no_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    section_start = no_comments.find(".view[hidden]")
    assert section_start != -1, "happiness CSS section not found after comment strip"
    section_no_comments = no_comments[section_start:]
    forbidden = ["green", "lime", "chartreuse", "seagreen", "mediumseagreen", "#52c41a"]
    for tok in forbidden:
        assert tok not in section_no_comments, (
            f"happiness section uses chromatic token {tok!r} outside of comments"
        )


def test_css_view_toggle_uses_hidden_attribute():
    css = (_STATIC / "annoyance.css").read_text(encoding="utf-8")
    assert ".view[hidden]" in css


# ── 4. Migration idempotent ──────────────────────────────────────────────────

def test_polarity_migration_is_idempotent(fresh_db):
    import migrations
    db.init_db()
    migrations.run_all()
    db.init_db()
    migrations.run_all()

    cols = db._table_columns(db._get_conn(), "spikes")
    assert "polarity" in cols

    indexes = {
        r[1] for r in db._get_conn().execute(
            "PRAGMA index_list(spikes)"
        ).fetchall()
    }
    assert "idx_spikes_polarity" in indexes


def test_pre_existing_spikes_default_to_negative(fresh_db):
    """Backward compat: spike inserted without explicit polarity → 'negative'."""
    import migrations
    migrations.run_all()

    db.insert_spike(
        entity="LegacyCo", detected_hour=_hour_iso(0),
        z_score=3.5, multiple_of_baseline=4.0, avg_annoyance=70.0,
        count=10, sample_post_ids=[],
    )
    with db.cursor() as cur:
        row = cur.execute(
            "SELECT polarity FROM spikes WHERE entity = ?", ("LegacyCo",)
        ).fetchone()
    assert row["polarity"] == "negative"

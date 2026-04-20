"""Unit tests for the false-positive flag DB helpers.

Uses the ``fresh_db`` fixture from conftest — every test gets a clean
temp sqlite so assertions are independent.
"""

from __future__ import annotations

import db


def _seed_spike() -> int:
    """Insert one spike and return its id. Shared across tests."""
    spike_id = db.insert_spike(
        entity="TestCorp",
        detected_hour=db.current_hour_iso(),
        z_score=4.2,
        multiple_of_baseline=3.7,
        avg_annoyance=78.0,
        count=12,
        sample_post_ids=["t:1", "t:2"],
        summary="test summary",
        sample_excerpts=["Something annoying", "More of the same"],
        confidence_score=68.5,
        sources_breakdown=[{"source": "reddit", "count": 8}, {"source": "bluesky", "count": 4}],
    )
    assert spike_id is not None
    return spike_id


class TestFpFlag:
    def test_insert_writes_row(self, fresh_db):
        spike_id = _seed_spike()
        flag_id = db.insert_fp_flag(
            spike_id=spike_id,
            user_id="42",
            user_email="reviewer@example.com",
            reason="entity misclassified",
        )
        assert isinstance(flag_id, int) and flag_id > 0

        flags = db.list_fp_queue(resolved=False)
        assert len(flags) == 1
        f = flags[0]
        assert f["spike_id"] == spike_id
        assert f["user_email"] == "reviewer@example.com"
        assert f["reason"] == "entity misclassified"
        assert f["resolved"] == 0
        # list_fp_queue joins the spike — we should see the entity inline.
        assert f["entity"] == "TestCorp"

    def test_list_filters_resolved(self, fresh_db):
        spike_id = _seed_spike()
        f1 = db.insert_fp_flag(spike_id=spike_id, user_id="u1", user_email="a@x.com", reason="r1")
        f2 = db.insert_fp_flag(spike_id=spike_id, user_id="u2", user_email="b@x.com", reason="r2")

        # Resolve one; the default list should now exclude it, and the
        # resolved=True list should include only it.
        assert db.resolve_fp_flag(f1, note="not a fp after all")

        open_flags = db.list_fp_queue(resolved=False)
        assert {f["id"] for f in open_flags} == {f2}

        done_flags = db.list_fp_queue(resolved=True)
        assert {f["id"] for f in done_flags} == {f1}
        assert done_flags[0]["resolution_note"] == "not a fp after all"

    def test_resolve_marks_row(self, fresh_db):
        spike_id = _seed_spike()
        flag_id = db.insert_fp_flag(spike_id=spike_id, user_id="u1", user_email="a@x.com", reason="r")

        # First call flips the row and returns True
        assert db.resolve_fp_flag(flag_id, note="resolved") is True
        # Second call is a no-op (already resolved), returns False
        assert db.resolve_fp_flag(flag_id, note="second pass") is False

        # And the row state matches what we expect
        done = db.list_fp_queue(resolved=True)
        assert len(done) == 1
        assert done[0]["resolved"] == 1
        assert done[0]["resolution_note"] == "resolved"
        assert done[0]["resolved_at"] is not None

"""Driver extraction on hand-computed fixtures.

3-model request, probability 0.62, stated weights sum to 1.00:
  deviations: race 0.02, state_polls 0.01, macro 0.15  (sum 0.18)
  movement shares: 1/9, 1/18, 5/6
  blend 50/50 with weight shares (0.34, 0.31, 0.35):
    race   0.5*0.34 + 0.5*(1/9)  = 0.225555... -> 0.2256
    polls  0.5*0.31 + 0.5*(1/18) = 0.182777... -> 0.1828
    macro  0.5*0.35 + 0.5*(5/6)  = 0.591666... -> 0.5917
  rounded sum would be 1.0001 > 1, so the excess 0.0001 comes off the
  largest driver: macro 0.5916. Final sum exactly 1.0.
"""

from __future__ import annotations

from narve_why.drivers import TAGS, extract_drivers, tag_for_model
from narve_why.schemas import ExplainRequest, ModelOutput


def _output(model_id: str, p: float, weight: float, refs: tuple[str, ...]) -> ModelOutput:
    return ModelOutput(
        model_id=model_id, source_id=model_id, p=p, weight=weight, inputs_ref=refs
    )


def _request(probability: float, outputs: tuple[ModelOutput, ...]) -> ExplainRequest:
    return ExplainRequest(
        question_id="q-1",
        question_text="Do Republicans win the U.S. House majority?",
        probability=probability,
        as_of="2026-07-01T09:00:00Z",
        model_outputs=outputs,
        market_snapshots=(),
    )


THREE_MODEL = _request(
    0.62,
    (
        _output("race_model", 0.64, 0.34, ("pred:1", "poll:az-0612")),
        _output("state_polls", 0.61, 0.31, ("poll:mi-0609",)),
        _output("macro_model", 0.47, 0.35, ("pred:9",)),
    ),
)


def test_hand_computed_weights_and_order() -> None:
    drivers = extract_drivers(THREE_MODEL)
    assert [(d.tag, d.weight) for d in drivers] == [
        ("economy", 0.5916),
        ("momentum", 0.2256),
        ("polling", 0.1828),
    ]
    assert sum(d.weight for d in drivers) <= 1.0


def test_hand_computed_directions() -> None:
    drivers = extract_drivers(THREE_MODEL)
    assert [d.direction for d in drivers] == ["down", "up", "down"]


def test_evidence_refs_carried_through() -> None:
    drivers = extract_drivers(THREE_MODEL)
    assert drivers[0].evidence_refs == ("pred:9",)
    assert drivers[1].evidence_refs == ("pred:1", "poll:az-0612")
    assert all(d.evidence_refs for d in drivers)


def test_labels_are_short_human_phrases() -> None:
    """Labels are the bare tag phrases (contract examples: "Weak incumbent
    approval") — direction is its own field and the question text must NOT be
    restated in every label (it made the prose unreadable)."""
    drivers = extract_drivers(THREE_MODEL)
    for driver in drivers:
        assert "Do Republicans win the U.S. House majority?" not in driver.label
        assert len(driver.label) <= 40
    assert drivers[1].label == "Model momentum"
    assert drivers[0].label == "Economic backdrop"


def test_tie_counts_as_up() -> None:
    req = _request(0.61, (_output("state_polls", 0.61, 1.0, ("poll:1",)),))
    drivers = extract_drivers(req)
    assert drivers[0].direction == "up"


def test_all_tags_valid() -> None:
    for driver in extract_drivers(THREE_MODEL):
        assert driver.tag in TAGS


def test_tag_explicit_mapping() -> None:
    assert tag_for_model("state_polls") == "polling"
    assert tag_for_model("generic_ballot") == "polling"
    assert tag_for_model("poll_aggregator") == "polling"
    assert tag_for_model("race_model") == "momentum"
    assert tag_for_model("macro_model") == "economy"
    assert tag_for_model("market_follow") == "market_structure"
    assert tag_for_model("incident_model") == "incident"


def test_tag_keyword_fallback() -> None:
    assert tag_for_model("exit_poll_feed") == "polling"
    assert tag_for_model("fundraising_totals") == "funding"
    assert tag_for_model("court_docket_model") == "legal"
    assert tag_for_model("legal_watch") == "legal"
    assert tag_for_model("econ_nowcast") == "economy"
    assert tag_for_model("macro_x") == "economy"
    assert tag_for_model("gov_shutdown_model") == "governance"
    assert tag_for_model("mystery_box") == "momentum"
    # keyword order is fixed: poll beats gov when both appear
    assert tag_for_model("gov_poll_hybrid") == "polling"


def test_zero_total_deviation_falls_back_to_weight_share() -> None:
    req = _request(
        0.5,
        (
            _output("a_model", 0.5, 0.6, ("ref:a",)),
            _output("b_model", 0.5, 0.4, ("ref:b",)),
        ),
    )
    drivers = extract_drivers(req)
    assert [d.weight for d in drivers] == [0.6, 0.4]
    assert [d.direction for d in drivers] == ["up", "up"]


def test_zero_total_weight_falls_back_to_uniform() -> None:
    req = _request(
        0.5,
        (
            _output("a_model", 0.6, 0.0, ("ref:a",)),
            _output("b_model", 0.4, 0.0, ("ref:b",)),
        ),
    )
    drivers = extract_drivers(req)
    assert [d.weight for d in drivers] == [0.5, 0.5]


def test_equal_weights_sorted_by_source_id() -> None:
    req = _request(
        0.5,
        (
            _output("b_model", 0.55, 0.5, ("ref:b",)),
            _output("a_model", 0.45, 0.5, ("ref:a",)),
        ),
    )
    drivers = extract_drivers(req)
    assert [d.evidence_refs[0] for d in drivers] == ["ref:a", "ref:b"]
    assert [d.weight for d in drivers] == [0.5, 0.5]


def test_weights_sum_never_exceeds_one() -> None:
    req = _request(
        0.62,
        (
            _output("race_model", 0.64, 1.02, ("pred:1",)),
            _output("state_polls", 0.61, 0.93, ("poll:2",)),
            _output("macro_model", 0.47, 1.05, ("pred:9",)),
        ),
    )
    drivers = extract_drivers(req)
    assert sum(d.weight for d in drivers) <= 1.0
    assert all(d.weight >= 0 for d in drivers)

"""Deterministic driver extraction — one driver per contributing model output.

Weight = 50/50 blend of normalised stated weight and share of absolute
deviation from the headline probability, rounded to 4dp (integer
ten-thousandths internally so the CONTRACTS.md guarantee sum(weights) <= 1
survives rounding: any rounding excess is shaved off the largest driver).
"""

from __future__ import annotations

from dataclasses import dataclass

from .schemas import ExplainRequest, ModelOutput

__all__ = [
    "TAGS",
    "TAG_LABELS",
    "TAG_OF_MODEL",
    "Driver",
    "extract_drivers",
    "tag_for_model",
]

TAGS = (
    "governance",
    "funding",
    "polling",
    "economy",
    "legal",
    "incident",
    "momentum",
    "market_structure",
)

TAG_OF_MODEL: dict[str, str] = {
    "state_polls": "polling",
    "generic_ballot": "polling",
    "poll_aggregator": "polling",
    "race_model": "momentum",
    "macro_model": "economy",
    "market_follow": "market_structure",
    "incident_model": "incident",
}

# Keyword fallback for unknown model ids, checked in this fixed order.
_KEYWORD_TAGS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("poll",), "polling"),
    (("fund",), "funding"),
    (("court", "legal"), "legal"),
    (("econ", "macro"), "economy"),
    (("gov",), "governance"),
)

TAG_LABELS: dict[str, str] = {
    "governance": "Governance and procedure",
    "funding": "Funding and money flows",
    "polling": "Polling",
    "economy": "Economic backdrop",
    "legal": "Legal and court developments",
    "incident": "Recent incidents",
    "momentum": "Model momentum",
    "market_structure": "Market structure",
}


@dataclass(frozen=True)
class Driver:
    tag: str
    label: str
    direction: str
    weight: float
    evidence_refs: tuple[str, ...]


def tag_for_model(model_id: str) -> str:
    """Deterministic model_id -> tag: exact map, then keyword, then momentum."""
    known = TAG_OF_MODEL.get(model_id)
    if known is not None:
        return known
    lowered = model_id.lower()
    for keywords, tag in _KEYWORD_TAGS:
        if any(keyword in lowered for keyword in keywords):
            return tag
    return "momentum"


def _label(tag: str, direction: str, question_text: str) -> str:
    # Short human phrase, per the contract's examples ("Weak incumbent
    # approval"). Direction is its own field; restating the question in every
    # label made the prose unreadable. question_text kept in the signature for
    # tag-specific phrasings that may want it later.
    _ = direction, question_text
    return TAG_LABELS[tag]


def extract_drivers(req: ExplainRequest) -> list[Driver]:
    """One driver per model output; sorted weight desc, then source_id asc."""
    outputs = req.model_outputs
    count = len(outputs)
    total_weight = sum(output.weight for output in outputs)
    deviations = [abs(output.p - req.probability) for output in outputs]
    total_deviation = sum(deviations)
    weighted: list[tuple[int, str, ModelOutput]] = []
    for output, deviation in zip(outputs, deviations):
        share_weight = output.weight / total_weight if total_weight > 0 else 1.0 / count
        share_move = deviation / total_deviation if total_deviation > 0 else share_weight
        blended = 0.5 * share_weight + 0.5 * share_move
        weighted.append((round(blended * 10000), output.source_id, output))
    weighted.sort(key=lambda item: (-item[0], item[1]))
    excess = sum(item[0] for item in weighted) - 10000
    if excess > 0:
        top = weighted[0]
        weighted[0] = (top[0] - excess, top[1], top[2])
        weighted.sort(key=lambda item: (-item[0], item[1]))
    drivers: list[Driver] = []
    for weight_int, _, output in weighted:
        tag = tag_for_model(output.model_id)
        direction = "up" if output.p >= req.probability else "down"
        drivers.append(
            Driver(
                tag=tag,
                label=_label(tag, direction, req.question_text),
                direction=direction,
                weight=weight_int / 10000,
                evidence_refs=output.inputs_ref,
            )
        )
    return drivers

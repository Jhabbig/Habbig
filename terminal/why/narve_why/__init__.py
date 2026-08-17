"""narve_why — the why engine for narve Terminal.

Explains a probability computed elsewhere; never computes or overrides it.
Public contract: repo-root CONTRACTS.md, Contract 1 (v1.0.0).

Names owned by report.py / prose.py / cli.py are exported lazily so the
deterministic core stays importable on its own.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .conflicts import CONFLICT_THRESHOLD_PTS, MIN_CREDIBILITY, Conflict, find_conflicts
from .credstate import (
    NEUTRAL_ALPHA,
    NEUTRAL_BETA,
    SourceState,
    load_db_state,
    load_fixture_state,
    state_for,
)
from .drivers import TAG_LABELS, TAG_OF_MODEL, TAGS, Driver, extract_drivers, tag_for_model
from .schemas import ContractError, ExplainRequest, MarketSnapshot, ModelOutput, parse_request
from .would_change import WOULD_CHANGE_PHRASES, would_change

__version__ = "1.0.0"

__all__ = [
    "BANNED",
    "CONFLICT_THRESHOLD_PTS",
    "Conflict",
    "ContractError",
    "Driver",
    "ExplainReport",
    "ExplainRequest",
    "MIN_CREDIBILITY",
    "MarketSnapshot",
    "ModelOutput",
    "NEUTRAL_ALPHA",
    "NEUTRAL_BETA",
    "SourceState",
    "TAGS",
    "TAG_LABELS",
    "TAG_OF_MODEL",
    "WOULD_CHANGE_PHRASES",
    "__version__",
    "explain",
    "extract_drivers",
    "find_conflicts",
    "llm_polish",
    "load_db_state",
    "load_fixture_state",
    "parse_request",
    "render_prose",
    "state_for",
    "tag_for_model",
    "to_json",
    "would_change",
]

_LAZY_EXPORTS: dict[str, str] = {
    "BANNED": "narve_why.prose",
    "ExplainReport": "narve_why.report",
    "explain": "narve_why.report",
    "llm_polish": "narve_why.prose",
    "render_prose": "narve_why.prose",
    "to_json": "narve_why.report",
    "main": "narve_why.cli",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module 'narve_why' has no attribute {name!r}")
    return getattr(import_module(module_name), name)

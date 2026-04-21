"""Claude-backed intelligence layer for narve.ai.

Public surface:

  ai.client    — async Anthropic SDK wrapper + usage logging
  ai.cache     — generic TTL cache keyed by sha-scoped strings
  ai.extractor — prediction extraction (Haiku)
  ai.categoriser      — market categorisation (Haiku)
  ai.source_summariser — source narrative (Sonnet)
  ai.environmental    — per-market CO2 impact (Sonnet)

Each module follows the same three-part pattern:
  - a thin async ``_call_claude`` that tests monkey-patch
  - a pure-function parser that turns the raw text into a validated dict
  - a public entrypoint that composes cache → call → parse → log → return

Nothing here imports server.py or touches request state. All DB access
goes through sqlite3 directly (via cache.py and each module's typed
helpers) so the intelligence layer stays testable in isolation.
"""

from ai.client import (  # noqa: F401
    ANTHROPIC_MODELS,
    cost_for,
    get_async_client,
    log_response,
    log_failure,
)

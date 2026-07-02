"""Message Batches queue for non-interactive metric extraction.

Cost control lever #3: the upstream metric-prediction LLM call dominates unit
cost, so non-interactive jobs (backfills, re-scores, replay enrichment) are
routed through the provider Batch API at 50% of standard price, with the
shared extraction system prompt marked for prompt caching (cached input bills
at ~0.1x). Results land in the same extraction_cache table the interactive
path reads, so every batched extraction is a future cache hit.

Config-gated (engine.batch.enabled); a missing API key makes the whole module
a graceful no-op. The scheduler drives flush/poll on
engine.batch.flush_interval_seconds when enabled.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional

from app.config import settings
from app.engine.config import get_engine_config
from app.engine.tiering import resolve_model_tier

logger = logging.getLogger(__name__)


def _get_async_client():
    api_key = settings.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    try:
        from anthropic import AsyncAnthropic
        return AsyncAnthropic(api_key=api_key)
    except ImportError:
        return None


def is_enabled() -> bool:
    return bool(get_engine_config().get("batch", {}).get("enabled")) and bool(settings.get("ANTHROPIC_API_KEY"))


class BatchExtractionQueue:
    def __init__(self) -> None:
        self._pending: Dict[str, str] = {}   # content_hash -> content
        self._submitted: Dict[str, str] = {}  # batch_id -> model used

    def enqueue(self, content: str) -> Optional[str]:
        """Queue a post/page for batched extraction. Returns its content hash."""
        if not content or not content.strip():
            return None
        from app.processing.llm_extractor import _hash_content
        content_hash = _hash_content(content)
        self._pending.setdefault(content_hash, content)
        return content_hash

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def flush(self) -> Optional[str]:
        """Submit up to max_batch_size pending items as one provider batch."""
        if not self._pending or not is_enabled():
            return None
        client = _get_async_client()
        if client is None:
            return None

        cfg = get_engine_config().get("batch", {})
        model = resolve_model_tier("batch")
        max_size = int(cfg.get("max_batch_size", 500))
        take = list(self._pending.items())[:max_size]

        from app.processing.llm_extractor import _SYSTEM_PROMPT
        requests = [
            {
                "custom_id": content_hash,  # sha256 hex — valid batch custom_id
                "params": {
                    "model": model,
                    "max_tokens": 1024,
                    # shared system prompt is the prompt-cache anchor (-90% on reads)
                    "system": [{
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    "messages": [{"role": "user", "content": content[:4000]}],
                },
            }
            for content_hash, content in take
        ]
        try:
            batch = await client.messages.batches.create(requests=requests)
        except Exception as exc:
            logger.error("Batch submit failed (%d items kept queued): %s", len(take), exc)
            return None
        for content_hash, _ in take:
            self._pending.pop(content_hash, None)
        self._submitted[batch.id] = model
        logger.info("Submitted extraction batch %s (%d items, model=%s)", batch.id, len(take), model)
        return batch.id

    async def poll(self) -> dict:
        """Collect finished batches; write results into the extraction cache."""
        stats = {"batches_checked": 0, "batches_completed": 0, "results_cached": 0, "errors": 0}
        if not self._submitted:
            return stats
        client = _get_async_client()
        if client is None:
            return stats

        from app.processing.extractor import ExtractionResult
        from app.processing.llm_extractor import _write_cache

        for batch_id in list(self._submitted):
            model = self._submitted[batch_id]
            stats["batches_checked"] += 1
            try:
                batch = await client.messages.batches.retrieve(batch_id)
                if batch.processing_status != "ended":
                    continue
                async for result in await client.messages.batches.results(batch_id):
                    if result.result.type != "succeeded":
                        stats["errors"] += 1
                        continue
                    message = result.result.message
                    text = next((b.text for b in message.content if b.type == "text"), "")
                    extractions = _parse_extraction_json(text)
                    if extractions is None:
                        stats["errors"] += 1
                        continue
                    results = [
                        ExtractionResult(
                            predicted_outcome=p.get("predicted_outcome", "Yes"),
                            predicted_probability=p.get("predicted_probability"),
                            raw_text=str(p.get("raw_text", ""))[:200],
                            extraction_method="llm_batch",
                            category=p.get("category", "other"),
                        )
                        for p in extractions
                        if float(p.get("confidence", 1.0)) >= 0.5
                    ]
                    await _write_cache(result.custom_id, model, results)
                    stats["results_cached"] += 1
                self._submitted.pop(batch_id, None)
                stats["batches_completed"] += 1
            except Exception as exc:
                logger.warning("Batch %s poll failed: %s", batch_id, exc)
        return stats

    async def run_cycle(self) -> dict:
        """One scheduler tick: submit anything pending, then harvest results."""
        submitted = await self.flush()
        stats = await self.poll()
        stats["submitted_batch"] = submitted
        return stats


def _parse_extraction_json(text: str) -> Optional[List[dict]]:
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    predictions = payload.get("predictions") if isinstance(payload, dict) else None
    if not isinstance(predictions, list):
        return None
    return [p for p in predictions if isinstance(p, dict)]


batch_queue = BatchExtractionQueue()

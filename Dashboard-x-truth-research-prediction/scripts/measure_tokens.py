#!/usr/bin/env python3
"""Measure real tokens in/out for a single metric-prediction job.

"The one number that changes everything": at concurrent scale, the token
count of one extraction call decides whether $/prediction is viable. This
script runs ONE live extraction against the configured model, prints the
measured usage (including prompt-cache reads on a second call), and prices it
against the engine cost table — interactive, batch (-50%), and warm-cache
variants. Instrument first, build second.

    ANTHROPIC_API_KEY=... python scripts/measure_tokens.py
    ANTHROPIC_API_KEY=... python scripts/measure_tokens.py --model claude-haiku-4-5
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make `app` importable


import argparse
import asyncio

SAMPLE_POST = (
    "BTC will hit 150k by end of year, I'd put it at 70% — the ETF inflows "
    "plus the halving supply squeeze make this the most asymmetric bet on the board."
)


async def run(model: str | None) -> int:
    from app.config import settings
    from app.engine.metrics import compute_cost_usd
    from app.processing.llm_extractor import _SYSTEM_PROMPT

    if not settings.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set — cannot measure live token usage.")
        print("Set the key and re-run; this number should anchor the cost model before scaling.")
        return 1

    from anthropic import AsyncAnthropic
    from app.engine.tiering import resolve_model_tier

    model = model or resolve_model_tier("interactive")
    client = AsyncAnthropic(api_key=settings["ANTHROPIC_API_KEY"])

    async def one_call():
        return await client.messages.create(
            model=model,
            max_tokens=1024,
            system=[{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": SAMPLE_POST}],
        )

    print(f"Measuring one metric-prediction job on {model} ...")
    first = await one_call()
    second = await one_call()  # same prefix — shows the prompt-cache read rate

    for label, resp in (("cold (cache write)", first), ("warm (cache read)", second)):
        u = resp.usage
        cached = getattr(u, "cache_read_input_tokens", 0) or 0
        created = getattr(u, "cache_creation_input_tokens", 0) or 0
        total_in = u.input_tokens + cached + created
        cost = compute_cost_usd(model, total_in, u.output_tokens, cached_tokens_in=cached)
        cost_batch = compute_cost_usd(model, total_in, u.output_tokens, cached_tokens_in=cached, batch=True)
        print(f"\n[{label}]")
        print(f"  input tokens (uncached/cache-read/cache-write): {u.input_tokens}/{cached}/{created}")
        print(f"  output tokens: {u.output_tokens}")
        print(f"  cost interactive: ${cost:.6f}   batch (-50%): ${cost_batch:.6f}")
        print(f"  → $/1k jobs: interactive ${cost * 1000:.2f}   batch ${cost_batch * 1000:.2f}")

    print("\nPaste the warm-path number into your capacity plan; the engine's")
    print("dedup cache turns repeated content into $0 jobs on top of this.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=None, help="override the interactive tier model")
    args = parser.parse_args()
    return asyncio.run(run(args.model))


if __name__ == "__main__":
    sys.exit(main())

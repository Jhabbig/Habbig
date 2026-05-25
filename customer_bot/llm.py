"""Optional LLM polish for drafted outreach copy.

When ANTHROPIC_API_KEY is set, runs each candidate draft through Claude
Haiku 4.5 to make it sound like a person who built the tool answering a
specific question — instead of a templated bot reply.

Strict constraints:
  • The URL and ref code from the template must survive verbatim.
  • Max 80 words.
  • No emojis, no marketing speak, no "I think you'll love it".
  • One CTA (the URL). One sign-off line at most.
  • If the LLM call fails for any reason, fall back to the template.

Prompt caching is on the system prompt + topic block, so per-cycle cost
drops by ~90% for the 2nd+ lead per dashboard.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Optional

from customer_bot.config import DashboardTopic
from customer_bot.lead import RawLead

log = logging.getLogger("customer_bot.llm")

# Loaded lazily so the bot still runs if the SDK isn't installed.
_client = None
_client_init_attempted = False

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 250

SYSTEM_BASE = (
    "You polish outreach drafts for narve.ai, a personal SaaS project that "
    "runs prediction-market and trading dashboards. Your output replaces "
    "the draft entirely.\n\n"
    "Hard rules — violating any of these is failure:\n"
    "  • Keep the URL exactly as it appears in the draft. Do not change the "
    "    subdomain, path, or ?ref= query string. Do not add UTM tags.\n"
    "  • Max 80 words.\n"
    "  • No emojis. No exclamation marks. No marketing speak ('powerful', "
    "    'cutting-edge', 'revolutionary'). No 'I hope this helps'.\n"
    "  • Reference the actual post — quote a phrase from it if it helps make "
    "    the reply feel specific, not templated.\n"
    "  • One sentence framing it as a personal project ('I built it', "
    "    'made this for myself'). Do not say 'we' or 'our team'.\n"
    "  • End with the URL on its own line. No sign-off after the URL.\n"
    "  • Output is the polished message only — no preamble, no quotes around "
    "    it, no markdown fences."
)


def is_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _get_client():
    global _client, _client_init_attempted
    if _client_init_attempted:
        return _client
    _client_init_attempted = True
    if not is_available():
        return None
    try:
        from anthropic import AsyncAnthropic
        _client = AsyncAnthropic()
        log.info("LLM draft polish enabled (model=%s)", MODEL)
    except ImportError:
        log.warning("ANTHROPIC_API_KEY set but `anthropic` package not installed; LLM polish disabled")
        _client = None
    return _client


_URL_RE = re.compile(r"https?://[^\s)]+")


def _extract_url(draft: str) -> Optional[str]:
    m = _URL_RE.search(draft or "")
    return m.group(0) if m else None


async def polish_draft(template_draft: str, lead: RawLead, topic: DashboardTopic) -> str:
    """Return a polished version of `template_draft`, or the original on failure."""
    client = _get_client()
    if client is None:
        return template_draft

    required_url = _extract_url(template_draft)
    if not required_url:
        # If the template has no URL, polishing risks losing the ref entirely.
        return template_draft

    snippet = (lead.body or lead.title or "")[:600]
    # Topic block is per-dashboard, so a cycle that produces 5 crypto leads
    # only pays the input-token cost once thanks to the cache breakpoint.
    system_blocks = [
        {"type": "text", "text": SYSTEM_BASE, "cache_control": {"type": "ephemeral"}},
        {"type": "text",
         "text": f"Dashboard pitch you're responding from:\n{topic.pitch}\n\n"
                 f"Required URL (must appear verbatim, on the final line):\n{required_url}",
         "cache_control": {"type": "ephemeral"}},
    ]
    user_msg = (
        f"Source: {lead.source} ({lead.context_label})\n"
        f"Author handle: {lead.author or 'unknown'}\n"
        f"Post / comment text:\n---\n{snippet}\n---\n\n"
        f"Current draft to rewrite:\n---\n{template_draft}\n---\n\n"
        f"Rewrite the draft into the final outreach message."
    )

    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system_blocks,
                messages=[{"role": "user", "content": user_msg}],
            ),
            timeout=20.0,
        )
    except asyncio.TimeoutError:
        log.warning("LLM polish timed out for lead %s", lead.source_id)
        return template_draft
    except Exception as exc:  # noqa: BLE001 — anthropic raises a variety of types
        log.warning("LLM polish failed for lead %s: %s", lead.source_id, exc)
        return template_draft

    try:
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    except Exception:
        return template_draft

    if not text or required_url not in text:
        # Model dropped the URL — fall back rather than send a CTA-less message.
        log.info("LLM polish dropped URL for %s; using template", lead.source_id)
        return template_draft

    return text

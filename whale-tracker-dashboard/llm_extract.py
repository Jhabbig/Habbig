"""Domain-specific LLM extractors for SEC filings.

Two extractors:

  extract_activist_intent(filing_text) → SC 13D/G filer's intent + demands
  extract_ma_terms(filing_text)        → 8-K M&A deal terms

We try to pull the most relevant section from the filing body before
sending it to the model so we stay well under context (and inference
time) limits. For 13D that's Item 4 ("Purpose of Transaction"); for 8-K
M&A it's Item 1.01 or Item 2.01.
"""

from __future__ import annotations

import logging
import re

import llm_client

log = logging.getLogger("llm_extract")

MAX_EXCERPT_CHARS = 16_000   # ~4k tokens worth — generous on context


_TAG_RX     = re.compile(r"<[^>]+>")
_SCRIPT_RX  = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_WS_RX      = re.compile(r"\s+")


def strip_html(s: str) -> str:
    if not s:
        return ""
    s = _SCRIPT_RX.sub(" ", s)
    s = _TAG_RX.sub(" ", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#160;", " ")
    return _WS_RX.sub(" ", s).strip()


def _excerpt(text: str, markers: tuple[str, ...], max_chars: int = MAX_EXCERPT_CHARS) -> str:
    """Find the first occurrence of any marker and return up to max_chars
    from that point. Falls back to the leading max_chars if no marker hits."""
    if not text:
        return ""
    lowered = text.lower()
    for m in markers:
        idx = lowered.find(m.lower())
        if idx >= 0:
            return text[idx:idx + max_chars]
    return text[:max_chars]


# ─── Activist intent (SC 13D / 13G) ───────────────────────────────────

ACTIVIST_SYSTEM = """You are a financial analyst extracting structured data from US SEC Schedule 13D and 13G filings.
You will receive an excerpt — typically Item 4 ("Purpose of Transaction") or the cover page.

Return ONLY a single JSON object with these keys. Use null (or empty array) when info is not in the text. Do not invent.

{
  "intent": one of:
      "passive"             - 13G or pure investment, no influence intent stated
      "activist_governance" - seeking board seats, governance changes
      "activist_strategic"  - pushing strategic actions (spin-off, capital return, M&A)
      "sale_or_breakup"     - explicitly wants company sold, taken private, or broken up
      "block"               - defensive / blocking stake to oppose a transaction
      "other"               - doesn't fit the above
  "demands": ["board seat", "strategic review", "dividend", ...]  // short strings; [] if none
  "prior_history_mentioned": boolean,
  "fund_type": "hedge_fund" | "private_equity" | "activist_fund" | "family_office" | "passive_index" | "other" | null,
  "confidence": float in [0,1],
  "summary": "1-2 sentence plain-English summary of the filer's stated intent."
}
"""


async def extract_activist_intent(filing_text: str) -> dict | None:
    body = strip_html(filing_text)
    excerpt = _excerpt(body, markers=("Item 4", "Purpose of Transaction"))
    if not excerpt:
        return None
    return await llm_client.chat_json(
        system=ACTIVIST_SYSTEM,
        user=f"Filing excerpt:\n\n{excerpt}",
    )


# ─── M&A deal terms (8-K) ─────────────────────────────────────────────

MA_SYSTEM = """You are a financial analyst extracting structured M&A deal terms from US SEC 8-K filings.
You will receive an excerpt — typically Item 1.01 ("Entry into a Material Definitive Agreement"), Item 2.01 ("Completion of Acquisition"), or the leading section of the filing.

Return ONLY a single JSON object with these keys. Use null when info is not in the text. Do not invent.

{
  "is_definitive_agreement": boolean,
  "deal_type": "merger" | "acquisition" | "tender_offer" | "spinoff" | "asset_sale" | "joint_venture" | "other" | null,
  "target_name": string | null,
  "target_ticker": string | null,
  "acquirer_name": string | null,
  "acquirer_ticker": string | null,
  "consideration_type": "cash" | "stock" | "mixed" | null,
  "consideration_per_share_usd": float | null,    // cash $ per target share
  "exchange_ratio": float | null,                 // acquirer shares per target share
  "implied_premium_pct": float | null,
  "termination_fee_usd": float | null,            // in millions if available, else absolute $
  "expected_close": string | null,                // "YYYY", "YYYY-QN", or "YYYY-MM"
  "summary": "1-2 sentence plain-English summary of the deal."
}
"""


async def extract_ma_terms(filing_text: str) -> dict | None:
    body = strip_html(filing_text)
    excerpt = _excerpt(body, markers=("Item 1.01", "Item 2.01", "definitive agreement",
                                      "agreement and plan of merger", "tender offer"))
    if not excerpt:
        return None
    return await llm_client.chat_json(
        system=MA_SYSTEM,
        user=f"Filing excerpt:\n\n{excerpt}",
    )

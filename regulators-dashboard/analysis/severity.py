"""Fine-amount extraction and severity bucketing for v0.2.

For text mentioning a fine/penalty/settlement, extract the monetary amount
and bucket it on a USD-equivalent scale. Multi-currency (USD / GBP / EUR)
with approximate FX — bucket thresholds are 10× apart so 20% FX moves
don't shift a bucket.

Approach:
  1. Find every "context word" occurrence in the text (fine, penalty,
     settle, pay, disgorge, restitution …).
  2. For each, scan a `PROXIMITY`-char window on either side for a
     monetary amount.
  3. Parse the amount → native value → USD-equivalent.
  4. Bucket the maximum USD-equivalent across all valid (context, amount)
     pairs into `low / medium / high / severe`.

The context-word anchor is the false-positive guard: a press release that
quotes "$10 billion quarterly profit" with no enforcement context will not
trigger a severity. This is by design — we'd rather miss-tag than
mis-attribute.

Limitations to keep in mind:
  - English-only context words. Translated text from non-English
    regulators (BaFin, JFSA) needs a per-language phrase list.
  - Single-amount pickup per item. "Multiple defendants ordered to pay
    different sums" surfaces the largest. That's the most punitive
    reading, which is also what's editorially useful.
  - FX is fixed at module load. For a 100M-USD vs 80M-USD borderline
    in a different currency, the bucket is approximate. The dashboard
    is a screen for spotting things to read further — exact precision
    isn't its job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Approximate FX. Bucket thresholds are 10× apart so 20% FX moves don't
# shift buckets. Refresh annually if it matters.
FX_TO_USD: dict[str, float] = {"USD": 1.00, "GBP": 1.25, "EUR": 1.10}

SYMBOL_TO_CCY: dict[str, str] = {"$": "USD", "£": "GBP", "€": "EUR"}

# Multiplier for magnitude suffix words. Keys are lowercased.
MAG_MULT: dict[str, float] = {
    "thousand": 1e3, "k": 1e3,
    "million": 1e6, "m": 1e6, "mn": 1e6, "mil": 1e6, "mm": 1e6,
    "billion": 1e9, "b": 1e9, "bn": 1e9, "bil": 1e9,
}

# Phrases that anchor an amount to an enforcement action. Case-insensitive,
# whole-word matching.
CONTEXT_WORDS_RX = re.compile(
    r"\b("
    r"fines?|fined|penalt(?:y|ies)|"
    r"settle(?:s|d|ment)?|"
    r"pays?|paying|paid|"
    r"disgorg(?:e|ed|ement)|"
    r"restitution"
    r")\b",
    re.IGNORECASE,
)

# Money amount with optional currency symbol, optional ISO code, required
# numeric body, optional magnitude word. We post-filter to reject bare
# numbers (no symbol, no code, no magnitude) so "year 2025" doesn't match.
AMOUNT_RX = re.compile(
    r"""
    (?:(?P<sym>[\$£€])\s*|(?P<ccy>USD|GBP|EUR)\s+)?
    (?P<num>\d[\d,]*(?:\.\d+)?)
    \s*
    (?P<mag>billion|million|thousand|bn|mn|mil|bil|mm|[kmb])?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# (bucket name, low USD inclusive, high USD exclusive)
BUCKETS: list[tuple[str, float, float]] = [
    ("low",     0.0,             1_000_000.0),
    ("medium",  1_000_000.0,    10_000_000.0),
    ("high",    10_000_000.0,  100_000_000.0),
    ("severe",  100_000_000.0,  float("inf")),
]

# Characters between the context word and the amount.
PROXIMITY = 80


@dataclass
class Severity:
    bucket: str            # "low" | "medium" | "high" | "severe"
    amount_usd: float
    amount_native: float
    currency: str          # "USD" | "GBP" | "EUR"
    amount_text: str       # raw substring as it appeared
    context_word: str      # the matched anchor (lowercased)

    def to_dict(self) -> dict:
        return {
            "bucket": self.bucket,
            "amount_usd": round(self.amount_usd, 2),
            "amount_native": round(self.amount_native, 2),
            "currency": self.currency,
            "amount_text": self.amount_text,
            "context_word": self.context_word,
        }


def _bucket_for(amount_usd: float) -> str:
    for name, lo, hi in BUCKETS:
        if lo <= amount_usd < hi:
            return name
    return "low"  # 0 / negative falls back


def _parse_amount(match: re.Match) -> tuple[float, float, str, str] | None:
    sym = match.group("sym")
    ccy_iso = match.group("ccy")
    num_raw = match.group("num")
    mag_raw = match.group("mag")

    # Reject bare numbers — no currency context, no magnitude. This guards
    # against picking up dates ("2025"), section numbers, page counts, etc.
    if not sym and not ccy_iso and not mag_raw:
        return None

    if sym:
        currency = SYMBOL_TO_CCY[sym]
    elif ccy_iso:
        currency = ccy_iso.upper()
    else:
        # Magnitude-only ("100 million", "5 thousand") — caller asserts the
        # amount is near a fine/penalty word, so USD is the safe default for
        # SEC press releases. UK/EU regulators usually attach a currency
        # symbol.
        currency = "USD"

    try:
        amount = float(num_raw.replace(",", ""))
    except ValueError:
        return None

    if mag_raw:
        mult = MAG_MULT.get(mag_raw.lower())
        if mult is None:
            return None
        amount *= mult

    if amount <= 0:
        return None

    usd = amount * FX_TO_USD.get(currency, 1.0)
    return amount, usd, currency, match.group(0).strip()


def extract(text: str) -> Severity | None:
    """Returns the largest enforcement amount in `text`, or None."""
    if not text:
        return None
    best: Severity | None = None
    for ctx_match in CONTEXT_WORDS_RX.finditer(text):
        ctx_word = ctx_match.group(0).lower()
        ctx_start, ctx_end = ctx_match.span()
        win_start = max(0, ctx_start - PROXIMITY)
        win_end = min(len(text), ctx_end + PROXIMITY)
        for amt_match in AMOUNT_RX.finditer(text, win_start, win_end):
            parsed = _parse_amount(amt_match)
            if not parsed:
                continue
            amount_native, amount_usd, currency, amount_text = parsed
            cand = Severity(
                bucket=_bucket_for(amount_usd),
                amount_usd=amount_usd,
                amount_native=amount_native,
                currency=currency,
                amount_text=amount_text,
                context_word=ctx_word,
            )
            if best is None or cand.amount_usd > best.amount_usd:
                best = cand
    return best


# --- Self-test --------------------------------------------------------------

_FIXTURES: list[tuple[str, str | None]] = [
    ("SEC charges firm with fraud and orders $1.5 million disgorgement",       "medium"),
    ("FCA fines bank £50 million for AML failings",                             "high"),
    ("Goldman to pay $200 million civil penalty",                               "severe"),
    ("Firm to pay $5,000 in restitution",                                       "low"),
    ("Court orders $1.2 billion settlement",                                    "severe"),
    ("Settlement of GBP 25 million announced",                                  "high"),
    ("ESMA enforcement: €5 million fine",                                       "medium"),
    ("Defendant ordered to disgorge $250,000 plus prejudgment interest",        "low"),
    # Negatives — no enforcement context, or no amount near context
    ("SEC adopts rules requiring climate-related disclosures",                  None),
    ("Speech by Chair on the future of market structure",                       None),
    ("Quarterly profits hit $10 billion at JPMorgan",                           None),
    ("Wells notice issued to firm without monetary terms",                      None),
    # Multi-amount — should pick the largest
    ("Defendants to pay $5,000 in restitution and $200 million in penalties",   "severe"),
]


if __name__ == "__main__":
    pass_count = 0
    for text, expected in _FIXTURES:
        sev = extract(text)
        got = sev.bucket if sev else None
        ok = got == expected
        pass_count += int(ok)
        mark = "✓" if ok else "✗"
        sev_repr = (
            f"{sev.amount_text!r:25s} ≈ ${sev.amount_usd:>15,.0f}  ctx={sev.context_word!r}"
            if sev else "—"
        )
        print(f"{mark} expected={str(expected):8s}  got={str(got):8s}  | {sev_repr}  | {text}")
    print(f"\n{pass_count}/{len(_FIXTURES)} fixtures pass")

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from app.config import yaml_config
from app.models import CategoryEnum

logger = logging.getLogger(__name__)

_scoring_cfg = yaml_config.get("scoring", {})
MARKET_MATCH_THRESHOLD = _scoring_cfg.get("market_match_threshold", 0.50)
MIN_SHARED_TOKENS = _scoring_cfg.get("min_shared_tokens", 3)
NEAR_PERFECT_SCORE = 0.95


@dataclass
class ExtractionResult:
    predicted_outcome: str
    predicted_probability: Optional[float]
    raw_text: str
    extraction_method: str
    category: str = "other"


# Explicit probability statements. Each pattern's group(1) is the percentage.
PERCENTAGE_PATTERNS = [
    re.compile(r"(?:about|around|~|roughly)?\s*(\d{1,3})\s*%\s*(?:chance|probability|likely|likelihood|odds)", re.IGNORECASE),
    re.compile(r"(?:I(?:'d| would)\s+(?:say|put it at|estimate|give it))\s+(?:about\s+|around\s+)?(\d{1,3})\s*%", re.IGNORECASE),
    re.compile(r"(\d{1,3})\s*%\s*(?:sure|certain|confident)", re.IGNORECASE),
    # "the odds/chance of X (happening) is/are at 40%"
    re.compile(r"\b(?:chance|odds|probability|likelihood)\s+(?:of|that|are|is)\b[^%\n]{0,80}?(?:\bat\s+|\bis\s+|\bare\s+|\baround\s+|\babout\s+)?(\d{1,3})\s*%", re.IGNORECASE),
    # "I give it/the Chiefs a 65% (chance)"
    re.compile(r"\bgive\w*\s+(?:it|this|that|him|her|them|the\s+\w+|\w+)\s+(?:a\s+|an\s+)?(\d{1,3})\s*%", re.IGNORECASE),
    # spelled out: "30 percent chance"
    re.compile(r"(\d{1,3})\s*(?:percent|pct)\s*(?:chance|probability|likely|likelihood|sure|certain|confident|odds)", re.IGNORECASE),
]

# "1 in 5 chance" \u2014 groups are (numerator, denominator).
FRACTION_ODDS_PATTERN = re.compile(
    r"\b(\d{1,2})\s+in\s+(\d{1,3})\s+(?:chance|shot|odds|probability|likelihood)\b", re.IGNORECASE
)

# When an explicit probability co-occurs with one of these, the speaker is
# giving the probability of the event NOT happening the stated way \u2014
# "90% sure this won't pass" means No @ 0.90, not Yes.
PCT_NEGATION_PATTERN = re.compile(
    r"\b(?:won['\u2019]t|will\s+not|isn['\u2019]t\s+going\s+to|not\s+going\s+to"
    r"|never\s+(?:happen|pass|hold|win|close|materiali[sz]e|get|make|recover)\w*"
    r"|doesn['\u2019]t\s+(?:happen|pass|hold|win))\b",
    re.IGNORECASE,
)

_FUTURE_EVENT_VERBS = r"(?:win|pass|happen|succeed|be approved|get (?:elected|approved|done)|launch|hit|reach|cut|hold|close|resign|default|survive|recover)"

DIRECTIONAL_POSITIVE = [
    re.compile(r"\b(?:will|going to|gonna)\b.{0,50}\b" + _FUTURE_EVENT_VERBS + r"\b", re.IGNORECASE),
    re.compile(r"\b(?:definitely|certainly|absolutely|no doubt)\b.{0,50}\b(?:will|going to|gonna)\b", re.IGNORECASE),
    re.compile(r"\bbet(?:ting)?\s+on\b", re.IGNORECASE),
    re.compile(r"\bmy prediction[:\s]+.+\bwill\b", re.IGNORECASE),
    re.compile(r"\bI (?:predict|think|believe|expect)\b.{0,50}\bwill\b", re.IGNORECASE),
]

# Negative direction requires an event verb nearby \u2014 a bare "never" or
# "impossible" anywhere in a post is not a prediction ("I never said that").
DIRECTIONAL_NEGATIVE = [
    re.compile(r"\b(?:will\s+not|won['\u2019]t|can['\u2019]t|doesn['\u2019]t|isn['\u2019]t\s+going\s+to|not\s+going\s+to)\b.{0,60}\b(?:" + _FUTURE_EVENT_VERBS + r"|make\s+it)\b", re.IGNORECASE),
    re.compile(r"\b(?:no\s+way|zero\s+chance|no\s+chance|impossible)\b.{0,80}\b(?:will|going\s+to|gonna|wins?|passes?|happens?|succeeds?|holds?)\b", re.IGNORECASE),
    re.compile(r"\b(?:will|going\s+to|gonna)\s+never\b|\bnever\s+(?:happen|pass|hold|win|close|survive|recover|make)\w*\b", re.IGNORECASE),
]

CONDITIONAL_PATTERN = re.compile(r"\bif\b.{5,60}\bthen\b.{5,60}\bwill\b", re.IGNORECASE)

# Verbal probability lexicon \u2014 ordered most-specific first; the value is
# P(event resolves YES) implied by the phrase.
WORD_PROBABILITIES = [
    (re.compile(r"\b(?:almost|near(?:ly)?|virtually)\s+certain(?:ly)?\b", re.IGNORECASE), 0.92),
    (re.compile(r"\b(?:highly|very|extremely)\s+likely\b", re.IGNORECASE), 0.85),
    (re.compile(r"\bmore\s+likely\s+than\s+not\b", re.IGNORECASE), 0.60),
    (re.compile(r"\b(?:coin\s*flip|toss[-\s]?up|50[-/]50)\b", re.IGNORECASE), 0.50),
    (re.compile(r"\b(?:highly|very|extremely)\s+unlikely\b", re.IGNORECASE), 0.15),
    (re.compile(r"\b(?:long\s+shot|slim\s+chance)\b", re.IGNORECASE), 0.12),
    (re.compile(r"\b(?:no|zero)\s+chance\b|\bimpossible\b", re.IGNORECASE), 0.03),
]

_WORD_PROB_EVENT_CONTEXT = re.compile(
    r"\b(?:win|wins|pass|passes|happen|happens|hold|holds|cut|cuts|approve[sd]?|close[sd]?|reach(?:es)?|hit|hits|elect(?:ed|ion)?|succeed|survive)\b",
    re.IGNORECASE,
)

# Hard vetoes: the post can never be a prediction (quotes of someone else,
# promos, "will X ever ...?" rhetoric). These also gate the LLM path.
HARD_FALSE_POSITIVE_PATTERNS = [
    re.compile(r"\bwill\s+\w+\s+ever\b.+\?", re.IGNORECASE),
    re.compile(r'^["\u201c].+["\u201d]$'),
    re.compile(r"\b(?:sale|discount|off|coupon|promo|buy now)\b", re.IGNORECASE),
]

# Soft vetoes: past-tense reporting. Skipped when the post ALSO carries an
# explicit probability \u2014 "they won game one, 70% chance they take the series"
# is a real prediction. NOTE: won(?!['\u2019]t) so "won't" isn't eaten as "won".
SOFT_FALSE_POSITIVE_PATTERNS = [
    re.compile(r"\b(?:won(?!['\u2019]t)|lost|passed|happened|succeeded|failed|was elected)\b", re.IGNORECASE),
    re.compile(r"\b(?:dropped|rose|increased|decreased|up|down)\s+\d+(?:\.\d+)?%", re.IGNORECASE),
]

MIN_WORDS_GENERAL = 10   # directional/keyword paths need real context
MIN_WORDS_EXPLICIT = 4   # an explicit % + a named event can be short

# Backwards-compat alias (older callers/tests import FALSE_POSITIVE_PATTERNS).
FALSE_POSITIVE_PATTERNS = HARD_FALSE_POSITIVE_PATTERNS + SOFT_FALSE_POSITIVE_PATTERNS

_prediction_keywords = yaml_config.get("scraping", {}).get("keywords", {}).get("prediction_keywords", [])
_category_keywords = yaml_config.get("scraping", {}).get("keywords", {}).get("category_keywords", {})

STOP_WORDS = frozenset("the a an is are was were will be to of and or in on at for with that this it i we they he she my your his her do does did have has had not no but can could would should may might shall its me us them our their been being if so very what who which when where how all each every some any many much".split())


def _tokenize(text: str) -> set[str]:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return {t for t in text.split() if t not in STOP_WORDS and len(t) > 1}


def _jaccard(tokens_a: set[str], tokens_b: set[str]) -> float:
    """Jaccard similarity, with a minimum-overlap floor.

    Returns 0.0 when fewer than MIN_SHARED_TOKENS tokens are shared — this
    kills coincidental 1-2 word overlaps that would otherwise dominate short
    texts (the root cause of the Bulgarian-election / LeBron mismatch).
    """
    if not tokens_a or not tokens_b:
        return 0.0
    overlap = len(tokens_a & tokens_b)
    if overlap < MIN_SHARED_TOKENS:
        return 0.0
    union = len(tokens_a | tokens_b)
    return overlap / union if union > 0 else 0.0


def fuzzy_match_score(text_a: str, text_b: str) -> float:
    return _jaccard(_tokenize(text_a), _tokenize(text_b))


def _outcome_appears_in(prediction_tokens: set[str], outcome_name: str) -> bool:
    """True if every word of ``outcome_name`` appears in the prediction tokens.

    Used to gate multi-outcome markets so a prediction about Trump can't get
    matched to the Harris market just because the question stems are 90%
    identical. Multi-word outcomes ("RFK Jr.") require ALL tokens to appear
    so we don't match "Jr." standalone.
    """
    name_tokens = _tokenize(outcome_name)
    if not name_tokens:
        return True  # No outcome to gate on → don't filter
    return name_tokens.issubset(prediction_tokens)


def match_to_market(
    prediction_text: str,
    markets: list[dict],
    threshold: float | None = None,
    category: str | None = None,
) -> tuple[dict | None, float]:
    """Match a prediction to the best candidate market.

    Strictly filters by category when one is given: a politics prediction
    will never match a sports market, even if no politics markets exist
    (in which case `(None, 0.0)` is returned).

    For multi-outcome markets (those with a non-null ``outcome_name``), the
    outcome name must appear as a whole-word match in the prediction text —
    otherwise the candidate is skipped. This fixes the failure mode where a
    "Trump will win" prediction matched the Harris market because the
    question stems share ~90% of their tokens in an N-candidate event.
    """
    if threshold is None:
        threshold = MARKET_MATCH_THRESHOLD
    if category and category != CategoryEnum.other.value:
        markets = [m for m in markets if m.get("category") == category]
        if not markets:
            return (None, 0.0)
    pred_tokens = _tokenize(prediction_text)
    best: tuple[dict | None, float] = (None, 0.0)
    for m in markets:
        # Multi-outcome gating — skip candidates whose outcome name isn't named
        # in the prediction. Binary markets (outcome_name = None or empty) pass.
        outcome_name = (m.get("outcome_name") or "").strip()
        if outcome_name and not _outcome_appears_in(pred_tokens, outcome_name):
            continue
        mkt_tokens = m.get("_tokens")
        if mkt_tokens is None:
            mkt_tokens = _tokenize(m.get("market_question", "") or m.get("question", ""))
        score = _jaccard(pred_tokens, mkt_tokens)
        if score > best[1]:
            best = (m, score)
            if score >= NEAR_PERFECT_SCORE:
                break
    return best if best[1] >= threshold else (None, 0.0)


def infer_category(text: str) -> str:
    text_lower = text.lower()
    best_cat, best_count = "other", 0
    for cat, keywords in _category_keywords.items():
        count = 0
        for kw in keywords:
            if len(kw) <= 4:
                if re.search(r'\b' + re.escape(kw.lower()) + r'\b', text_lower):
                    count += 1
            else:
                if kw.lower() in text_lower:
                    count += 1
        if count > best_count:
            best_count = count
            best_cat = cat
    return best_cat


def _in_question_sentence(content: str, match_end: int) -> bool:
    """True if the sentence containing a match ends with '?'.

    "Will the Lakers win the title this year?" is a question, not a
    prediction — a directional pattern firing inside it is a false positive.
    """
    q = content.find("?", match_end)
    if q == -1:
        return False
    return not re.search(r"[.!\n]", content[match_end:q])


def _word_probability(content: str) -> Optional[float]:
    """P(YES) implied by a verbal-probability phrase, if one is present."""
    for pat, prob in WORD_PROBABILITIES:
        if pat.search(content):
            return prob
    return None


class PredictionExtractor:
    def _extract_explicit(self, content: str) -> list[ExtractionResult]:
        """Explicit probability statements: percentages and fraction odds.

        These carry enough signal on their own that short posts qualify and
        past-tense soft vetoes don't apply. If the post also negates the
        event ("90% sure this won't pass"), the outcome flips to No with the
        stated probability kept — the speaker is that sure of the No side.
        """
        if len(content.split()) < MIN_WORDS_EXPLICIT:
            return []
        outcome = "No" if PCT_NEGATION_PATTERN.search(content) else "Yes"
        category = None  # inferred lazily, once
        results: list[ExtractionResult] = []
        seen_spans: list[tuple[int, int]] = []

        def _overlaps(start: int, end: int) -> bool:
            return any(s < end and start < e for s, e in seen_spans)

        for pat in PERCENTAGE_PATTERNS:
            m = pat.search(content)
            if not m or _overlaps(*m.span(1)):
                continue
            try:
                pct = int(m.group(1))
            except (ValueError, IndexError):
                continue
            if not 0 <= pct <= 100:
                continue
            prob = min(max(pct / 100.0, 0.02), 0.98)
            if category is None:
                category = infer_category(content)
            seen_spans.append(m.span(1))
            results.append(ExtractionResult(outcome, prob, m.group(0).strip(), "percentage", category))

        m = FRACTION_ODDS_PATTERN.search(content)
        if m and not _overlaps(*m.span()):
            try:
                num, den = int(m.group(1)), int(m.group(2))
                if 0 < num < den <= 100:
                    if category is None:
                        category = infer_category(content)
                    results.append(ExtractionResult(outcome, round(num / den, 4), m.group(0).strip(), "fraction_odds", category))
            except (ValueError, IndexError):
                pass

        return results

    def _extract_regex(self, content: str) -> list[ExtractionResult]:
        """Precise regex / pattern path. High precision, low recall."""
        if not content:
            return []

        for fp in HARD_FALSE_POSITIVE_PATTERNS:
            if fp.search(content):
                return []

        # Explicit probabilities outrank every veto below: a short post or a
        # past-tense preamble doesn't make "70% chance we close the series"
        # any less of a prediction.
        results = self._extract_explicit(content)
        if results:
            return results

        if len(content.split()) < MIN_WORDS_GENERAL:
            return []
        for fp in SOFT_FALSE_POSITIVE_PATTERNS:
            if fp.search(content):
                return []

        for pat in DIRECTIONAL_NEGATIVE:
            m = pat.search(content)
            if m and not _in_question_sentence(content, m.end()):
                word_prob = _word_probability(content)
                # For a No prediction only the low band is unambiguous:
                # "no chance/long shot/unlikely" state P(YES) directly.
                prob = word_prob if word_prob is not None and word_prob <= 0.30 else None
                return [ExtractionResult("No", prob, m.group(0).strip(), "directional", infer_category(content))]

        for pat in DIRECTIONAL_POSITIVE:
            m = pat.search(content)
            if m and not _in_question_sentence(content, m.end()):
                return [ExtractionResult("Yes", _word_probability(content), m.group(0).strip(), "directional", infer_category(content))]

        # A verbal-probability phrase next to an event verb is itself a
        # prediction: "the challenger is a long shot to win the runoff".
        for pat, prob in WORD_PROBABILITIES:
            m = pat.search(content)
            if m and not _in_question_sentence(content, m.end()) and _WORD_PROB_EVENT_CONTEXT.search(content):
                return [ExtractionResult("Yes", prob, m.group(0).strip(), "word_probability", infer_category(content))]

        m = CONDITIONAL_PATTERN.search(content)
        if m:
            return [ExtractionResult("Yes", None, m.group(0).strip(), "conditional", infer_category(content))]

        return []

    def _extract_keyword_fallback(self, content: str) -> list[ExtractionResult]:
        """Last-resort keyword match. Low precision — used only when no LLM is available."""
        content_lower = content.lower()
        for kw in _prediction_keywords:
            if kw.lower() in content_lower:
                return [ExtractionResult("Yes", None, content[:200], "keyword", infer_category(content))]
        return []

    def _vetoed_for_llm(self, content: str) -> bool:
        """Whether the post is a waste of an LLM call.

        Hard vetoes always gate. Soft (past-tense) vetoes gate only when the
        post carries no future marker at all — "they won last night, book the
        repeat" is exactly the kind of post the LLM disambiguates well.
        """
        for fp in HARD_FALSE_POSITIVE_PATTERNS:
            if fp.search(content):
                return True
        has_future_marker = re.search(
            r"\b(?:will|going\s+to|gonna|chance|odds|predict|bet|expect|forecast|likely)\b",
            content, re.IGNORECASE,
        )
        if not has_future_marker:
            for fp in SOFT_FALSE_POSITIVE_PATTERNS:
                if fp.search(content):
                    return True
        return False

    def extract(self, content: str) -> list[ExtractionResult]:
        """Synchronous regex + keyword fallback (legacy callers + unit tests)."""
        if not content:
            return []
        results = self._extract_regex(content)
        if results:
            return results
        if len(content.split()) < MIN_WORDS_GENERAL:
            return []
        return self._extract_keyword_fallback(content)

    async def extract_async(self, content: str) -> list[ExtractionResult]:
        """Regex first, LLM second, keyword last.

        The regex path is fast and free; we only pay the LLM when nothing
        precise matched. Keyword fallback is below the LLM because it produces
        a generic "Yes" with no probability and noisy categorization — fine as
        a fallback when the LLM is unavailable, but the LLM strictly dominates it.
        """
        if not content or len(content.split()) < MIN_WORDS_EXPLICIT:
            return []
        results = self._extract_regex(content)
        if results:
            return results
        if self._vetoed_for_llm(content):
            return []
        try:
            from app.processing import llm_extractor
            if llm_extractor.is_available():
                llm_results = await llm_extractor.extract(content)
                if llm_results:
                    return llm_results
        except Exception as exc:
            logger.warning("LLM extractor invocation failed: %s", exc)
        if len(content.split()) < MIN_WORDS_GENERAL:
            return []
        return self._extract_keyword_fallback(content)

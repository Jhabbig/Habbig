from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from app.config import yaml_config

logger = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    predicted_outcome: str
    predicted_probability: Optional[float]
    raw_text: str
    extraction_method: str
    category: str = "other"


PERCENTAGE_PATTERNS = [
    re.compile(r"(?:about|around|~|roughly)?\s*(\d{1,3})\s*%\s*(?:chance|probability|likely|likelihood)", re.IGNORECASE),
    re.compile(r"(?:I(?:'d| would)\s+(?:say|put it at|estimate|give it))\s+(?:about\s+|around\s+)?(\d{1,3})\s*%", re.IGNORECASE),
    re.compile(r"(\d{1,3})\s*%\s*(?:sure|certain|confident)", re.IGNORECASE),
]

DIRECTIONAL_POSITIVE = [
    re.compile(r"\b(?:will|going to|gonna)\b.{0,50}\b(?:win|pass|happen|succeed|be approved|get elected|launch|hit)\b", re.IGNORECASE),
    re.compile(r"\b(?:definitely|certainly|absolutely|no doubt)\b.{0,50}\b(?:will|going to|gonna)\b", re.IGNORECASE),
    re.compile(r"\bbet(?:ting)?\s+on\b", re.IGNORECASE),
    re.compile(r"\bmy prediction[:\s]+.+\bwill\b", re.IGNORECASE),
    re.compile(r"\bI (?:predict|think|believe|expect)\b.{0,50}\bwill\b", re.IGNORECASE),
]

DIRECTIONAL_NEGATIVE = [
    re.compile(r"\b(?:will not|won't|won\'t|no way|never|impossible|zero chance)\b", re.IGNORECASE),
    re.compile(r"\b(?:won't|can't|doesn't|isn't going to|not going to)\b.{0,50}\b(?:win|pass|happen|succeed)\b", re.IGNORECASE),
    re.compile(r"\bno chance\b", re.IGNORECASE),
]

CONDITIONAL_PATTERN = re.compile(r"\bif\b.{5,60}\bthen\b.{5,60}\bwill\b", re.IGNORECASE)

FALSE_POSITIVE_PATTERNS = [
    re.compile(r"\bwill\s+\w+\s+ever\b.+\?", re.IGNORECASE),
    re.compile(r"\b(?:won|lost|passed|happened|succeeded|failed|was elected)\b", re.IGNORECASE),
    re.compile(r'^["\u201c].+["\u201d]$'),
    re.compile(r"\b(?:sale|discount|off|coupon|promo|buy now)\b", re.IGNORECASE),
    re.compile(r"\b(?:dropped|rose|increased|decreased|up|down)\s+\d+%", re.IGNORECASE),
]

_prediction_keywords = yaml_config.get("scraping", {}).get("keywords", {}).get("prediction_keywords", [])
_category_keywords = yaml_config.get("scraping", {}).get("keywords", {}).get("category_keywords", {})

STOP_WORDS = frozenset("the a an is are was were will be to of and or in on at for with that this it i we they he she my your his her do does did have has had not no but can could would should may might shall its me us them our their been being if so very what who which when where how all each every some any many much".split())


def _tokenize(text: str) -> set[str]:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return {t for t in text.split() if t not in STOP_WORDS and len(t) > 1}


MIN_SHARED_TOKENS = 3


def fuzzy_match_score(text_a: str, text_b: str) -> float:
    tokens_a, tokens_b = _tokenize(text_a), _tokenize(text_b)
    if not tokens_a or not tokens_b:
        return 0.0
    overlap = len(tokens_a & tokens_b)
    if overlap < MIN_SHARED_TOKENS:
        return 0.0
    union = len(tokens_a | tokens_b)
    return overlap / union if union > 0 else 0.0


def match_to_market(
    prediction_text: str,
    markets: list[dict],
    threshold: float | None = None,
    category: str | None = None,
) -> tuple[dict | None, float]:
    if threshold is None:
        threshold = yaml_config.get("scoring", {}).get("market_match_threshold", 0.50)
    # Pre-filter by category when available — prevents cross-category mismatches
    if category and category != "other":
        filtered = [m for m in markets if m.get("category") == category]
        if filtered:
            markets = filtered
    best: tuple[dict | None, float] = (None, 0.0)
    for m in markets:
        q = m.get("market_question", "") or m.get("question", "")
        score = fuzzy_match_score(prediction_text, q)
        if score > best[1]:
            best = (m, score)
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


class PredictionExtractor:
    def extract(self, content: str) -> list[ExtractionResult]:
        if not content or len(content.split()) < 10:
            return []
        for fp in FALSE_POSITIVE_PATTERNS:
            if fp.search(content):
                return []
        results: list[ExtractionResult] = []

        for pat in PERCENTAGE_PATTERNS:
            m = pat.search(content)
            if m:
                try:
                    pct = int(m.group(1))
                    if 1 <= pct <= 99:
                        results.append(ExtractionResult("Yes", pct / 100.0, m.group(0).strip(), "percentage", infer_category(content)))
                except (ValueError, IndexError):
                    pass
        if results:
            return results

        for pat in DIRECTIONAL_NEGATIVE:
            m = pat.search(content)
            if m:
                return [ExtractionResult("No", None, m.group(0).strip(), "directional", infer_category(content))]

        for pat in DIRECTIONAL_POSITIVE:
            m = pat.search(content)
            if m:
                return [ExtractionResult("Yes", None, m.group(0).strip(), "directional", infer_category(content))]

        m = CONDITIONAL_PATTERN.search(content)
        if m:
            return [ExtractionResult("Yes", None, m.group(0).strip(), "conditional", infer_category(content))]

        content_lower = content.lower()
        for kw in _prediction_keywords:
            if kw.lower() in content_lower:
                return [ExtractionResult("Yes", None, content[:200], "keyword", infer_category(content))]

        return []

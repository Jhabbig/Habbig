"""Rule-based hawkish↔dovish scoring.

Algorithm:
  1. Lowercase the statement, split into sentences (period/exclamation/Q-mark).
  2. For each phrase in the dictionary, count occurrences (case-insensitive,
     whole-word boundaries where applicable).
  3. score_raw   = Σ (weight × count)
  4. score_norm  = score_raw / max(num_sentences, 1)
  5. Bucket into HAWKISH / NEUTRAL / DOVISH thresholds for display.

The matched phrases (with their weights and counts) are returned alongside
the score so the user can sanity-check what triggered it. That transparency
is the whole point of going rule-based.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .stance_keywords import HAWKISH, DOVISH, NEUTRAL

# Bucket thresholds (on normalized score)
_HAWK_THRESHOLD = 0.3
_DOVE_THRESHOLD = -0.3


@dataclass
class StanceResult:
    raw_score: float
    norm_score: float          # raw / sentence_count
    bucket: str                # "HAWKISH" | "NEUTRAL" | "DOVISH"
    sentence_count: int
    matches: list[dict]        # [{phrase, weight, count}]
    neutral_matches: list[str]

    def to_dict(self) -> dict:
        return {
            "raw_score": round(self.raw_score, 2),
            "norm_score": round(self.norm_score, 3),
            "bucket": self.bucket,
            "sentence_count": self.sentence_count,
            "matches": self.matches,
            "neutral_matches": self.neutral_matches,
        }


def _split_sentences(text: str) -> list[str]:
    # Naive but adequate. CB press releases are clean prose.
    parts = re.split(r"[.!?]+\s+", text or "")
    return [p for p in parts if p.strip()]


def _count_phrase(text_lower: str, phrase: str) -> int:
    # If the phrase is a single token, enforce word boundaries.
    # Multi-word phrases match as substrings (with leading/trailing word
    # boundary on the outer tokens to avoid false hits inside other words).
    pattern = re.escape(phrase.lower())
    if " " in phrase:
        # Anchor outer edges only
        regex = re.compile(rf"\b{pattern}\b")
    else:
        regex = re.compile(rf"\b{pattern}\b")
    return len(regex.findall(text_lower))


def score(text: str) -> StanceResult:
    text = text or ""
    text_lower = text.lower()
    sentences = _split_sentences(text)
    n_sent = max(len(sentences), 1)

    matches: list[dict] = []
    raw = 0.0

    for phrase, weight in HAWKISH.items():
        c = _count_phrase(text_lower, phrase)
        if c:
            raw += weight * c
            matches.append({"phrase": phrase, "weight": weight, "count": c})
    for phrase, weight in DOVISH.items():
        c = _count_phrase(text_lower, phrase)
        if c:
            raw += weight * c
            matches.append({"phrase": phrase, "weight": weight, "count": c})

    # Sort matches by absolute contribution desc (biggest signals first)
    matches.sort(key=lambda m: -abs(m["weight"] * m["count"]))

    neutral_hits = sorted({p for p in NEUTRAL if _count_phrase(text_lower, p) > 0})

    norm = raw / n_sent
    if norm >= _HAWK_THRESHOLD:
        bucket = "HAWKISH"
    elif norm <= _DOVE_THRESHOLD:
        bucket = "DOVISH"
    else:
        bucket = "NEUTRAL"

    return StanceResult(
        raw_score=raw,
        norm_score=norm,
        bucket=bucket,
        sentence_count=n_sent,
        matches=matches,
        neutral_matches=neutral_hits,
    )


# --- Self-test --------------------------------------------------------------

_FIXTURES = [
    ("hawkish exemplar",
     "Inflation remains elevated. The Committee judges that further tightening is "
     "appropriate to bring inflation back to its 2 percent objective. Wage pressures "
     "remain robust and the labor market is tight. The Committee will remain vigilant.",
     "HAWKISH"),
    ("dovish exemplar",
     "Inflation has continued to ease and is moderating toward the 2 percent objective. "
     "The Committee judges that the risks have moved into better balance. Disinflation "
     "is broadening. The Committee will remain patient and is prepared to support growth "
     "if downside risks materialize.",
     "DOVISH"),
    ("neutral exemplar",
     "Recent indicators suggest economic activity has been expanding at a solid pace. "
     "The Committee will continue to monitor incoming data carefully. Decisions will "
     "be made meeting by meeting based on the totality of information received.",
     "NEUTRAL"),
]


if __name__ == "__main__":
    for name, txt, expected in _FIXTURES:
        r = score(txt)
        ok = "✓" if r.bucket == expected else "✗"
        print(f"{ok} {name:20s} expected={expected:8s} got={r.bucket:8s} "
              f"norm={r.norm_score:+.2f}  matches={len(r.matches)}")
        for m in r.matches[:3]:
            print(f"      {m['phrase']!r:30s} w={m['weight']:+d}  ×{m['count']}")

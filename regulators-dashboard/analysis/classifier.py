"""Rule-based regulator-action classifier.

Algorithm:
  1. Lowercase input (`title + " " + summary`).
  2. For each category dict in `classifier_keywords.CATEGORIES`, count
     occurrences of every phrase (case-insensitive, word-boundary anchored).
  3. category_score = Σ (weight × count) per category.
  4. `tags` = every category with score > 0, sorted by score desc.
  5. `primary` = `tags[0]` if any, else `"other"`.

Returns matched phrases per-category alongside the tags so the UI can
expose them on hover. That transparency is the entire point of going
rule-based — a reader can see exactly which words flipped a headline
into a category, and `classifier_keywords.py` is one edit away from
fixing a misclassification.

Same shape as `centralbank-dashboard/analysis/stance_scorer.py`, adapted
from a single-axis (hawkish↔dovish) score to a multi-class score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .classifier_keywords import CATEGORIES
from .severity import extract as extract_severity


@dataclass
class ClassifyResult:
    primary: str                          # one of CATEGORIES keys, or "other"
    tags: list[str]                       # all categories scoring > 0, desc by score
    scores: dict[str, int]                # raw score per category
    matches: dict[str, list[dict]]        # category → [{phrase, weight, count}]

    def to_dict(self) -> dict:
        return {
            "primary": self.primary,
            "tags": self.tags,
            "scores": self.scores,
            "matches": self.matches,
        }


_PHRASE_CACHE: dict[str, re.Pattern] = {}


def _phrase_regex(phrase: str) -> re.Pattern:
    rx = _PHRASE_CACHE.get(phrase)
    if rx is None:
        rx = re.compile(rf"\b{re.escape(phrase.lower())}\b")
        _PHRASE_CACHE[phrase] = rx
    return rx


def _count(text_lower: str, phrase: str) -> int:
    return len(_phrase_regex(phrase).findall(text_lower))


def classify(text: str) -> ClassifyResult:
    text_lower = (text or "").lower()
    scores: dict[str, int] = {}
    matches: dict[str, list[dict]] = {}

    for category, phrases in CATEGORIES.items():
        cat_score = 0
        cat_matches: list[dict] = []
        for phrase, weight in phrases.items():
            c = _count(text_lower, phrase)
            if c:
                cat_score += weight * c
                cat_matches.append({"phrase": phrase, "weight": weight, "count": c})
        scores[category] = cat_score
        cat_matches.sort(key=lambda m: -(m["weight"] * m["count"]))
        matches[category] = cat_matches

    tags = sorted(
        [c for c, s in scores.items() if s > 0],
        key=lambda c: -scores[c],
    )
    primary = tags[0] if tags else "other"
    return ClassifyResult(primary=primary, tags=tags, scores=scores, matches=matches)


def classify_item(item: dict) -> dict:
    """Run `classify()` on `title + summary` and attach result fields to the
    item in-place. Returns the same dict for caller convenience.

    Also runs v0.2 severity extraction. We always call `extract_severity`
    — the context-word anchor in `severity.py` is strict enough that
    non-enforcement items will return None on their own. Belt-and-braces:
    we still skip the severity field if the primary tag isn't enforcement,
    so a passing reference to "$5M revenue" in a rulemaking doc can't
    leak through if the regex ever loosens."""
    text = (item.get("title", "") + " " + item.get("summary", "")).strip()
    r = classify(text)
    item["primary_tag"] = r.primary
    item["tags"] = sorted(set(item.get("tags", []) + r.tags))
    item["matched_phrases"] = {
        cat: [m["phrase"] for m in mlist[:5]]
        for cat, mlist in r.matches.items()
        if mlist
    }
    if r.primary == "enforcement":
        sev = extract_severity(text)
        item["severity"] = sev.to_dict() if sev else None
    else:
        item["severity"] = None
    return item


# --- Self-test --------------------------------------------------------------

_FIXTURES = [
    ("SEC charges firm and CEO with fraud and orders disgorgement",                  "enforcement"),
    ("FCA fines bank £50 million for AML failings",                                  "enforcement"),
    ("ESMA proposes amendments to MiFID II regulatory technical standards",          "rulemaking"),
    ("SEC adopts rules requiring climate-related disclosures",                       "rulemaking"),
    ("FCA publishes guidance on cryptoasset financial promotions",                   "guidance"),
    ("ESMA issues Q&A on the Sustainable Finance Disclosure Regulation",             "guidance"),
    ("Speech by Chair on the future of market structure",                            "speech"),
    ("Testimony of Chair before the Senate Banking Committee",                       "speech"),
    ("SEC Commissioner sworn in for second term",                                    "personnel"),
    ("FCA appoints new Chief Executive",                                             "personnel"),
    ("ESMA publishes annual work programme for 2026",                                "other"),
]


if __name__ == "__main__":
    pass_count = 0
    for headline, expected in _FIXTURES:
        r = classify(headline)
        ok = r.primary == expected
        pass_count += int(ok)
        mark = "✓" if ok else "✗"
        top_matches = []
        for cat, mlist in r.matches.items():
            for m in mlist:
                top_matches.append(f"{cat}:{m['phrase']}×{m['count']}")
        print(f"{mark} expected={expected:11s}  got={r.primary:11s}  "
              f"score={r.scores.get(r.primary, 0):2d}  {headline}")
        if not ok:
            print(f"      scores={r.scores}")
            print(f"      matches={top_matches}")
    print(f"\n{pass_count}/{len(_FIXTURES)} fixtures pass")

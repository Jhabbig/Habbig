"""Topic-cluster extractor.

For each item's `title + summary`, count occurrences of every phrase in
the topic dictionary and emit the list of topics that score ≥ 1. An item
can fire multiple topics — "FCA fines crypto exchange for AML failings"
correctly tags both `crypto` and `aml`.

Same pattern as `classifier.py`: rule-based, transparent, matched phrases
exposed so the UI can show users why a topic fired.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .topic_keywords import TOPICS


@dataclass
class TopicResult:
    topics: list[str]                         # all firing topics, desc by score
    scores: dict[str, int]                    # raw score per topic
    matches: dict[str, list[dict]]            # topic → [{phrase, weight, count}]

    def to_dict(self) -> dict:
        return {
            "topics": self.topics,
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


def extract_topics(text: str) -> TopicResult:
    text_lower = (text or "").lower()
    scores: dict[str, int] = {}
    matches: dict[str, list[dict]] = {}

    for topic, phrases in TOPICS.items():
        topic_score = 0
        topic_matches: list[dict] = []
        for phrase, weight in phrases.items():
            c = _count(text_lower, phrase)
            if c:
                topic_score += weight * c
                topic_matches.append({"phrase": phrase, "weight": weight, "count": c})
        scores[topic] = topic_score
        topic_matches.sort(key=lambda m: -(m["weight"] * m["count"]))
        matches[topic] = topic_matches

    topics = sorted(
        [t for t, s in scores.items() if s > 0],
        key=lambda t: -scores[t],
    )
    return TopicResult(topics=topics, scores=scores, matches=matches)


def attach_topics(item: dict) -> dict:
    """Run `extract_topics()` on title + summary and attach to item in-place."""
    text = (item.get("title", "") + " " + item.get("summary", "")).strip()
    r = extract_topics(text)
    item["topics"] = r.topics
    item["matched_topic_phrases"] = {
        topic: [m["phrase"] for m in mlist[:5]]
        for topic, mlist in r.matches.items()
        if mlist
    }
    return item


# --- Self-test --------------------------------------------------------------

_FIXTURES = [
    # (headline, expected_topics_in_order_or_any_order)
    ("SEC charges crypto exchange with AML failings and customer due diligence violations",
        ["aml", "crypto"]),
    ("FCA approves first spot bitcoin ETF for retail investors",
        ["crypto", "etf"]),
    ("ESMA adopts technical standards on sustainability reporting and ESG disclosure",
        ["climate", "disclosure"]),
    ("SEC proposes amendments to private fund advisers under Form PF",
        ["disclosure", "privatefunds"]),
    ("FCA fines bank £50 million for cyber incident response failings",
        ["cyber"]),
    ("ESMA Q&A on market structure and best execution under MiFID II",
        ["marketstructure"]),
    ("SEC adopts climate-related disclosure rules requiring Scope 1, Scope 2 reporting",
        ["climate", "disclosure"]),
    ("Speech by Chair on the future of regulation",
        []),  # no topic fires
]


if __name__ == "__main__":
    pass_count = 0
    for headline, expected in _FIXTURES:
        r = extract_topics(headline)
        got = set(r.topics)
        want = set(expected)
        ok = got == want
        pass_count += int(ok)
        mark = "✓" if ok else "✗"
        print(f"{mark} expected={sorted(want)!s:35s} got={sorted(got)!s:35s} | {headline}")
        if not ok:
            print(f"      scores={r.scores}")
            for topic, mlist in r.matches.items():
                if mlist:
                    print(f"        {topic}: {[m['phrase'] for m in mlist]}")
    print(f"\n{pass_count}/{len(_FIXTURES)} fixtures pass")

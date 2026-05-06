"""Keyword dictionary for the v0.1 regulator-action classifier.

Six categories (`enforcement`, `rulemaking`, `speech`, `guidance`, `personnel`,
plus the implicit `other` fallback). Each phrase carries an integer weight:

    +N  — strong, less-ambiguous signal
    +1  — soft / contextual signal

Matching is case-insensitive whole-phrase (word-boundary anchored). Phrases
were drawn from real SEC / FCA / ESMA press-release headlines 2023–2026.

To extend behavior: add a phrase to the appropriate dict and re-run the
fixtures at the bottom of `classifier.py`. **This file is the supported
tuning surface** — same convention as
`centralbank-dashboard/analysis/stance_keywords.py`.

Cautions when adding phrases:
  - Single-word phrases match with `\\b…\\b` boundaries. Avoid common words
    that appear in unrelated titles ("names", "joins", "issues").
  - Short abbreviations (e.g. "rts", "its") are dangerous — they collide
    with English stop-words. Prefer the spelled-out form.
  - If a phrase strongly disambiguates two categories, weight it higher in
    one rather than spreading weight thin across both.
"""

from __future__ import annotations

ENFORCEMENT: dict[str, int] = {
    # Direct charging / settlement language
    "charges": 3,
    "charged": 3,
    "settles": 3,
    "settlement with": 3,
    "settle with": 2,
    "fines": 3,
    "fined": 3,
    "civil penalty": 3,
    "civil penalties": 3,
    "monetary penalty": 3,
    "wells notice": 3,
    "consent order": 3,
    "cease and desist": 3,
    "cease-and-desist": 3,
    "enforcement action": 3,
    "final judgment": 2,
    "obtains final judgment": 3,
    "convicted": 3,
    "indicted": 3,
    "guilty plea": 3,
    "pleads guilty": 3,
    "barred from": 3,
    "banned from": 3,
    "disgorgement": 3,
    "restitution": 2,
    "sanctioned": 2,
    "sanctions against": 3,

    # Common substantive offenses (enforcement context)
    "fraud": 2,
    "fraudulent": 2,
    "money laundering": 2,
    "insider trading": 3,
    "market manipulation": 3,
    "manipulation": 1,
    "misappropriation": 2,
    "ponzi": 3,

    # Soft / supporting
    "violation": 1,
    "violations": 1,
    "alleged": 1,
    "penalty": 1,
    "penalties": 1,
}

RULEMAKING: dict[str, int] = {
    "proposes": 3,
    "proposed rule": 3,
    "proposed rules": 3,
    "proposed amendment": 3,
    "proposed amendments": 3,
    "adopts rule": 3,
    "adopts rules": 3,
    "adopted rule": 2,
    "final rule": 3,
    "final rules": 3,
    "rule amendments": 2,
    "amends rule": 3,
    "amends rules": 3,
    "consultation paper": 3,
    "consultation on": 2,
    "open for comment": 2,
    "request for comment": 3,
    "request for comments": 3,
    "comment period": 2,
    "policy statement": 2,
    "regulatory technical standards": 3,
    "implementing technical standards": 3,
    "discussion paper": 2,
    "rulemaking": 3,
    "rule proposal": 3,
    "rules of procedure": 1,
}

SPEECH: dict[str, int] = {
    "speech by": 3,
    "remarks by": 3,
    "remarks at": 3,
    "address by": 2,
    "keynote": 3,
    "keynote address": 3,
    "testimony of": 3,
    "testimony before": 3,
    "testifies": 2,
    "to testify": 2,
    "speech": 2,
    "remarks": 1,  # 'remarks' alone is mild — boost via 'remarks by/at'
}

GUIDANCE: dict[str, int] = {
    "guidance": 2,
    "guidelines": 2,
    "supervisory statement": 3,
    "supervisory letter": 3,
    "interpretive release": 3,
    "interpretive guidance": 3,
    "no-action letter": 3,
    "no action letter": 3,
    "no-action relief": 3,
    "questions and answers": 3,
    "q&a": 3,
    "frequently asked questions": 3,
    "staff bulletin": 2,
    "staff statement": 2,
    "joint statement": 1,
    "warning": 1,
    "advisory": 1,
    "best practices": 2,
    "expectations": 1,
}

PERSONNEL: dict[str, int] = {
    "appointed": 3,
    "appoints": 3,
    "appointment of": 3,
    "named as": 3,
    "named to": 2,
    "nominated": 3,
    "nominates": 3,
    "nomination of": 3,
    "swears in": 3,
    "sworn in": 3,
    "departs": 3,
    "to depart": 3,
    "stepping down": 3,
    "to step down": 3,
    "steps down": 3,
    "resigns": 3,
    "resignation": 3,
    "to resign": 3,
    "retires": 3,
    "retirement of": 3,
    "successor": 2,
    "designated as": 2,
    "elected as": 2,
    "begins term": 2,
    "ends term": 2,
    "leaves the": 1,  # "leaves the SEC", "leaves the FCA"
}


CATEGORIES: dict[str, dict[str, int]] = {
    "enforcement": ENFORCEMENT,
    "rulemaking": RULEMAKING,
    "speech": SPEECH,
    "guidance": GUIDANCE,
    "personnel": PERSONNEL,
}

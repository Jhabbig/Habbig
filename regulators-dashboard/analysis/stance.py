"""Per-regulator speech stance scorer — v0.7.

Picks the most-recent `speech`-tagged item per regulator from the feed,
scores its title + summary against that regulator's `StanceAxis` from
`stance_keywords.py`, and exposes the bucketed stance + matched phrases.

Mechanics mirror `centralbank-dashboard/analysis/stance_scorer.py`:
  1. Lowercase + sentence-split.
  2. Σ (weight × count) over both positive and negative phrase dicts.
  3. Normalize by sentence count (so longer speeches don't mechanically
     score more extreme).
  4. Bucket: `|norm| ≥ threshold` → labeled pole, else `NEUTRAL`.
  5. Return matched phrases sorted by absolute contribution so the UI
     can show the top signals as chips.

Scope notes:
  - In v0.7 we score `title + summary` only — RSS summaries are usually
    enough to surface a stance signal but not all. Items where the score
    is dead-zero get `bucket=NEUTRAL` with `confidence=low`.
  - If a regulator has no recent speech in the feed window, we return
    `bucket=NO_SPEECH` so the UI can render "no recent speech" rather
    than implying neutrality.
  - Full-body fetching (matching what `cb_statements._fetch_statement_body`
    does for monetary policy releases) is a polish lift deferred until
    we see whether v0.7 signal is reliable enough on summaries alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .stance_keywords import AXES, StanceAxis

_PHRASE_CACHE: dict[str, re.Pattern] = {}


def _phrase_regex(phrase: str) -> re.Pattern:
    rx = _PHRASE_CACHE.get(phrase)
    if rx is None:
        rx = re.compile(rf"\b{re.escape(phrase.lower())}\b")
        _PHRASE_CACHE[phrase] = rx
    return rx


def _count(text_lower: str, phrase: str) -> int:
    return len(_phrase_regex(phrase).findall(text_lower))


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"[.!?]+\s+", text or "")
    return [p for p in parts if p.strip()]


@dataclass
class StanceResult:
    raw_score: float
    norm_score: float
    bucket: str               # positive_label, negative_label, or "NEUTRAL"
    sentence_count: int
    matches: list[dict]       # [{phrase, weight, count, side}]

    def to_dict(self) -> dict:
        return {
            "raw_score": round(self.raw_score, 2),
            "norm_score": round(self.norm_score, 3),
            "bucket": self.bucket,
            "sentence_count": self.sentence_count,
            "matches": self.matches,
        }


def score_text(text: str, axis: StanceAxis) -> StanceResult:
    text_lower = (text or "").lower()
    sentences = max(1, len(_split_sentences(text)))
    raw = 0.0
    matches: list[dict] = []
    for phrase, weight in axis.positive.items():
        c = _count(text_lower, phrase)
        if c:
            raw += weight * c
            matches.append({"phrase": phrase, "weight": weight, "count": c, "side": "positive"})
    for phrase, weight in axis.negative.items():
        c = _count(text_lower, phrase)
        if c:
            raw += weight * c
            matches.append({"phrase": phrase, "weight": weight, "count": c, "side": "negative"})
    matches.sort(key=lambda m: -abs(m["weight"] * m["count"]))
    norm = raw / sentences
    if norm >= axis.threshold:
        bucket = axis.positive_label
    elif norm <= -axis.threshold:
        bucket = axis.negative_label
    else:
        bucket = "NEUTRAL"
    return StanceResult(
        raw_score=raw,
        norm_score=norm,
        bucket=bucket,
        sentence_count=sentences,
        matches=matches,
    )


def compute(items: list[dict]) -> list[dict]:
    """Build the per-regulator stance ladder. Picks the most recent
    `speech`-tagged item per source from `items` and scores it.

    Returns a list ordered the same as `AXES` (SEC, FCA, ESMA in v0.7).
    Regulators without a recent speech in the feed get `bucket=NO_SPEECH`.
    """
    by_source: dict[str, list[dict]] = {}
    for it in items:
        src = it.get("source")
        if src:
            by_source.setdefault(src, []).append(it)

    out: list[dict] = []
    for code, axis in AXES.items():
        speeches = [it for it in by_source.get(code, []) if it.get("primary_tag") == "speech"]
        if not speeches:
            out.append({
                "regulator": code,
                "name": axis.name,
                "bucket": "NO_SPEECH",
                "norm_score": 0.0,
                "axis": {
                    "positive_label": axis.positive_label,
                    "negative_label": axis.negative_label,
                    "positive_long":  axis.positive_long,
                    "negative_long":  axis.negative_long,
                },
                "latest_speech": None,
                "matches": [],
            })
            continue
        latest = max(speeches, key=lambda x: x.get("published") or "")
        text = (latest.get("title", "") + " " + latest.get("summary", "")).strip()
        result = score_text(text, axis)
        out.append({
            "regulator": code,
            "name": axis.name,
            "bucket": result.bucket,
            "norm_score": round(result.norm_score, 3),
            "raw_score": round(result.raw_score, 2),
            "sentence_count": result.sentence_count,
            "axis": {
                "positive_label": axis.positive_label,
                "negative_label": axis.negative_label,
                "positive_long":  axis.positive_long,
                "negative_long":  axis.negative_long,
            },
            "latest_speech": {
                "title":     latest.get("title"),
                "link":      latest.get("link"),
                "published": latest.get("published"),
            },
            "matches": result.matches,
        })
    return out


# --- Self-test --------------------------------------------------------------

_FIXTURES = [
    # (regulator code, speech text, expected bucket)
    ("SEC",
     "We will pursue robust enforcement against fraudulent conduct. The Commission is committed "
     "to investor protection, holding bad actors accountable, and rooting out fraud in our markets.",
     "PRO_ENFORCEMENT"),
    ("SEC",
     "The Commission should reduce regulatory burden, ease compliance costs, modernize the rules, "
     "and streamline disclosure obligations to support pro-growth, innovation-friendly outcomes.",
     "LIGHT_TOUCH"),
    ("SEC",
     "Today the Commission released its annual work programme and welcomed the new commissioner.",
     "NEUTRAL"),
    ("FCA",
     "Our new secondary objective on growth and competitiveness is fundamental. Innovation, "
     "competition, and the regulatory sandbox are central to international competitiveness.",
     "PRO_INNOVATION"),
    ("FCA",
     "The Consumer Duty raises standards for consumer outcomes. We focus on consumer protection, "
     "fair value, and appropriate redress for vulnerable consumers harmed by predatory practices.",
     "CONSUMER_FIRST"),
    ("ESMA",
     "ESMA is publishing regulatory technical standards and binding guidelines to ensure uniform "
     "application across the Union. Single-rulebook harmonisation and supervisory convergence "
     "remain priorities.",
     "PRESCRIPTIVE"),
    ("ESMA",
     "A principles-based, proportionate, risk-based approach respects national discretion and "
     "subsidiarity. Outcomes-based supervision tailored to national contexts works best.",
     "PRINCIPLES_BASED"),
    # ── v2.4 new axes ──────────────────────────────────────────────────────
    ("BoE",
     "Financial stability is paramount. Our macroprudential tools and the countercyclical capital "
     "buffer guard against systemic risk. The stress test confirmed firms have robust resilience.",
     "PRO_STABILITY"),
    ("BoE",
     "The secondary objective on growth and competitiveness is reshaping our approach. We must "
     "remove barriers, support innovation, and improve international competitiveness.",
     "PRO_GROWTH"),
    ("CFTC",
     "We brought a vigorous enforcement action against spoofing and market manipulation, obtaining "
     "judgment with significant civil penalties and disgorgement.",
     "PRO_ENFORCEMENT"),
    ("CFTC",
     "Regulatory clarity is essential. Our pilot program and regulatory sandbox provide an "
     "innovation-friendly, principles-based framework with no-action letters.",
     "LIGHT_TOUCH"),
    ("Fed",
     "Inflationary pressures remain elevated. The Committee judges that further tightening is "
     "appropriate and policy must be sufficiently restrictive to bring inflation to target. "
     "The labor market is tight and the Committee will remain vigilant.",
     "HAWKISH"),
    ("Fed",
     "Disinflation is broadening. The Committee judges accommodative policy may be appropriate "
     "to support growth as softening data point to easing. Rate cuts are on the table.",
     "DOVISH"),
    ("ECB",
     "Policy must remain sufficiently restrictive. Underlying inflation is elevated and the "
     "transmission of monetary policy is ongoing. We remain vigilant on second-round effects.",
     "HAWKISH"),
    ("MAS",
     "We are advancing CBDC and tokenisation pilots. The regulatory sandbox and digital banking "
     "framework support fintech experimentation in Singapore as a global financial centre.",
     "PRO_INNOVATION"),
    ("MAS",
     "Financial stability and resilience are foundational. We remain vigilant against AML threats, "
     "scams, and vulnerable-consumer harm; prudential supervision is robust.",
     "PRO_STABILITY"),
]


if __name__ == "__main__":
    pass_count = 0
    for code, text, expected in _FIXTURES:
        axis = AXES[code]
        r = score_text(text, axis)
        ok = r.bucket == expected
        pass_count += int(ok)
        mark = "✓" if ok else "✗"
        top = ", ".join(m["phrase"] for m in r.matches[:3])
        print(f"{mark} {code:5s} expected={expected:18s} got={r.bucket:18s} "
              f"norm={r.norm_score:+.2f}  matches=[{top}]")
        if not ok:
            print(f"      all_matches={r.matches}")

    print(f"\n{pass_count}/{len(_FIXTURES)} fixtures pass")

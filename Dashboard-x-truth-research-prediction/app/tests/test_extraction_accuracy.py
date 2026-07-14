"""Labeled accuracy benchmark for the regex extraction path.

Each case is (post_text, expected) where expected is either None (the post
contains no tradeable prediction — the extractor must return []) or a dict
with the fields we grade:

    outcome  — "Yes" / "No"
    prob     — expected predicted_probability (graded with ±0.02 tolerance),
               or the sentinel ANY (a probability may or may not be attached),
               or None (no probability must be attached)

The suite grades every case and asserts an overall accuracy floor, so future
pattern changes that trade recall for precision (or vice versa) show up as a
number, not a vibe. Individual capability tests below the benchmark pin the
behaviors we rely on.

Run with:  pytest app/tests/test_extraction_accuracy.py -v
"""

from app.processing.extractor import PredictionExtractor

extractor = PredictionExtractor()

ANY = object()  # sentinel: probability may be anything (including None)

CASES = [
    # ── Explicit percentages, canonical phrasing ────────────────────────
    ("I think there is a 70% chance that Bitcoin will reach 100k by end of year, strong conviction here.",
     {"outcome": "Yes", "prob": 0.70}),
    ("Based on all the polling and turnout models I'd say 85% that the senate bill passes before recess.",
     {"outcome": "Yes", "prob": 0.85}),
    ("I'm 90% sure the merger gets approved by regulators before the end of this quarter.",
     {"outcome": "Yes", "prob": 0.90}),
    # ── Percentage variants the old patterns missed ─────────────────────
    ("BTC 150k by EOY, 70% chance.",  # short but high-signal
     {"outcome": "Yes", "prob": 0.70}),
    ("The odds of the ceasefire holding through March are at 40% in my view given the latest escalation.",
     {"outcome": "Yes", "prob": 0.40}),
    ("I give the Chiefs a 65% chance to repeat as champions this season, the roster is just better.",
     {"outcome": "Yes", "prob": 0.65}),
    ("There's maybe a 30 percent chance the Fed cuts in March, the inflation print changed everything.",
     {"outcome": "Yes", "prob": 0.30}),
    ("Realistically it's a 1 in 5 chance the bill passes this session with the current whip count.",
     {"outcome": "Yes", "prob": 0.20}),
    ("I'd put it at a 3 in 4 chance that the ETF gets approved before the deadline next month.",
     {"outcome": "Yes", "prob": 0.75}),
    # ── Negated percentages (outcome must flip) ─────────────────────────
    ("I'm 90% sure this bill won't pass the senate this year, the votes simply are not there.",
     {"outcome": "No", "prob": 0.90}),
    ("There's an 80% chance the deal never happens, both boards are dug in and financing is gone.",
     {"outcome": "No", "prob": 0.80}),
    # ── Directional positive ─────────────────────────────────────────────
    ("Trump will definitely win the Republican primary based on all the current polling and momentum.",
     {"outcome": "Yes", "prob": ANY}),
    ("I predict the Lakers will win the championship this year, they have the best roster in the league.",
     {"outcome": "Yes", "prob": ANY}),
    ("I'm betting on Ethereum to outperform Bitcoin this quarter, the fundamentals are clearly stronger.",
     {"outcome": "Yes", "prob": ANY}),
    # ── Directional negative ─────────────────────────────────────────────
    ("There is absolutely no way the infrastructure bill will pass the senate this session, votes aren't there.",
     {"outcome": "No", "prob": ANY}),
    ("The ceasefire won't hold through the winter, both sides are still shelling the corridor daily.",
     {"outcome": "No", "prob": ANY}),
    # ── Word probabilities attached to directional predictions ───────────
    ("The Fed will almost certainly cut rates at the March meeting, every governor has signaled it now.",
     {"outcome": "Yes", "prob": 0.92}),
    ("Honestly the election is a coin flip at this point, either candidate will win by a hair.",
     {"outcome": "Yes", "prob": 0.50}),
    ("The challenger is a long shot to win the runoff but the incumbent keeps making mistakes.",
     {"outcome": "Yes", "prob": 0.12}),
    # ── Past tense + future prediction in the same post (must extract) ───
    ("They won game one last night, and I'd say there's a 70% chance they close out the series in six.",
     {"outcome": "Yes", "prob": 0.70}),
    ("BTC dropped 5% today but there's an 80% chance we still hit 100k by March, dips get bought.",
     {"outcome": "Yes", "prob": 0.80}),
    # ── Non-predictions: must return [] ──────────────────────────────────
    ("Will Trump ever learn to stop tweeting controversial things? I really wonder about it sometimes honestly.", None),
    ("Will the Lakers win the title this year? I genuinely don't know what to make of this roster.", None),
    ("Biden won the election last November in a close race, the results were certified by all fifty states.", None),
    ("Amazing deal! Get 50% off all items in our store this weekend only, use promo code SAVE50 now.", None),
    ("BTC to 100k", None),  # too short, no explicit signal
    ("The market dropped 3% today on heavy volume, worst single session that we have seen since last autumn.", None),
    ("“The senate bill will pass easily next week”", None),  # pure quote of someone else
    ("I never said the merger was guaranteed, please stop misquoting my thread from last week, it's exhausting.", None),
]


def _grade(case):
    text, expected = case
    results = extractor.extract(text)
    if expected is None:
        return not results
    if not results:
        return False
    r = results[0]
    if r.predicted_outcome != expected["outcome"]:
        return False
    exp_prob = expected["prob"]
    if exp_prob is ANY:
        return True
    if exp_prob is None:
        return r.predicted_probability is None
    if r.predicted_probability is None:
        return False
    return abs(r.predicted_probability - exp_prob) <= 0.02


def test_benchmark_accuracy():
    """Overall accuracy floor across the labeled set."""
    failures = [(t[:60], extractor.extract(t)) for t, e in CASES if not _grade((t, e)) for t in [t]]
    correct = sum(1 for c in CASES if _grade(c))
    accuracy = correct / len(CASES)
    print(f"\nextraction benchmark: {correct}/{len(CASES)} = {accuracy:.0%}")
    for f in failures:
        print(f"  MISS: {f}")
    assert accuracy >= 0.92, f"extraction accuracy regressed: {accuracy:.0%} ({failures})"


# ── Pinned capability tests (deterministic, one behavior each) ──────────

def test_short_high_signal_post_extracted():
    r = extractor.extract("BTC 150k by EOY, 70% chance.")
    assert len(r) == 1 and r[0].predicted_probability == 0.70


def test_negated_percentage_flips_outcome():
    r = extractor.extract("I'm 90% sure this bill won't pass the senate this year, the votes simply are not there.")
    assert len(r) >= 1 and r[0].predicted_outcome == "No" and r[0].predicted_probability == 0.90


def test_fraction_odds():
    r = extractor.extract("Realistically it's a 1 in 5 chance the bill passes this session with the current whip count.")
    assert len(r) >= 1 and abs(r[0].predicted_probability - 0.20) < 0.01


def test_spelled_out_percent():
    r = extractor.extract("There's maybe a 30 percent chance the Fed cuts in March, the inflation print changed everything.")
    assert len(r) >= 1 and r[0].predicted_probability == 0.30


def test_word_probability_attached():
    r = extractor.extract("The Fed will almost certainly cut rates at the March meeting, every governor has signaled it now.")
    assert len(r) >= 1 and r[0].predicted_outcome == "Yes" and r[0].predicted_probability == 0.92


def test_question_sentence_not_a_prediction():
    assert extractor.extract("Will the Lakers win the title this year? I genuinely don't know what to make of this roster.") == []


def test_past_tense_with_future_signal_extracted():
    r = extractor.extract("They won game one last night, and I'd say there's a 70% chance they close out the series in six.")
    assert len(r) >= 1 and r[0].predicted_probability == 0.70


def test_bare_never_is_not_a_prediction():
    assert extractor.extract("I never said the merger was guaranteed, please stop misquoting my thread from last week, it's exhausting.") == []

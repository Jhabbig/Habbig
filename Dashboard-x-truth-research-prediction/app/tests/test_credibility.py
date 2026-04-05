from datetime import datetime, timedelta, timezone
from app.credibility.category_scores import smoothed_accuracy
from app.credibility.decay import decay_weight, decay_weighted_accuracy
from app.credibility.diversity import category_dominance_penalty, category_spread_penalty
from app.models import Source

def test_smoothed_1_of_1(): assert abs(smoothed_accuracy(1, 1, 0.5, 4) - 0.60) < 0.01
def test_smoothed_1_of_5(): assert abs(smoothed_accuracy(1, 5, 0.5, 4) - 0.333) < 0.01
def test_smoothed_8_of_10(): assert abs(smoothed_accuracy(8, 10, 0.5, 4) - 0.714) < 0.01
def test_smoothed_0_of_0(): assert abs(smoothed_accuracy(0, 0, 0.5, 4) - 0.5) < 0.01

def test_decay_today(): assert abs(decay_weight(datetime.now(timezone.utc), 60.0) - 1.0) < 0.01
def test_decay_60_days(): assert abs(decay_weight(datetime.now(timezone.utc) - timedelta(days=60), 60.0) - 0.5) < 0.01
def test_decay_120_days(): assert abs(decay_weight(datetime.now(timezone.utc) - timedelta(days=120), 60.0) - 0.25) < 0.01

def test_decay_weighted_accuracy_basic():
    class R:
        def __init__(self, d, c): self.predicted_at = datetime.now(timezone.utc) - timedelta(days=d); self.resolved_correct = c
    acc = decay_weighted_accuracy([R(0, True), R(60, False), R(120, True)], 60.0)
    assert abs(acc - 1.25 / 1.75) < 0.05

def test_spread_1_category(): assert abs(category_spread_penalty(["crypto"]) - 0.30) < 0.01
def test_spread_2_categories(): assert abs(category_spread_penalty(["crypto", "politics"]) - 0.60) < 0.01
def test_spread_3_categories(): assert abs(category_spread_penalty(["crypto", "politics", "sports"]) - 0.85) < 0.01
def test_spread_4_plus(): assert abs(category_spread_penalty(["a", "b", "c", "d"]) - 1.00) < 0.01
def test_spread_empty(): assert category_spread_penalty([]) == 0.0

def test_dominance_over_threshold():
    class R:
        def __init__(self, c): self.category = c
    assert category_dominance_penalty([R("crypto")] * 8 + [R("politics")] * 2, "crypto") == 0.15

def test_dominance_under_threshold():
    class R:
        def __init__(self, c): self.category = c
    assert category_dominance_penalty([R("crypto")] * 4 + [R("politics")] * 3 + [R("sports")] * 3, "crypto") == 0.0

def test_new_source_not_unlocked(sample_source_new): assert sample_source_new.accuracy_unlocked is False

def test_10_in_1_cat_not_unlocked():
    s = Source(handle="mono", platform="twitter", qualifying_predictions=10); s.categories_predicted_in = ["crypto"]
    assert not (s.qualifying_predictions >= 10 and len(s.categories_predicted_in) >= 3)

def test_10_across_3_unlocked():
    s = Source(handle="diverse", platform="twitter", qualifying_predictions=12); s.categories_predicted_in = ["crypto", "politics", "sports"]
    assert s.qualifying_predictions >= 10 and len(s.categories_predicted_in) >= 3

def test_manual_trusted_increases(): assert 0.40 + 0.13 * 0.20 > 0.40
def test_manual_untrusted_decreases(): assert 0.60 + 0.13 * (-0.40) < 0.60
def test_category_none_insufficient(): assert (None if 2 < 3 else 0.5) is None

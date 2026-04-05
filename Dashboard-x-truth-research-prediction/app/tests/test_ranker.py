from app.models import Prediction, Source
from app.processing.ranker import compute_ev_score, compute_risk_flags
from app.tests.conftest import NOW

def test_ev_positive(): assert abs(compute_ev_score(0.80, 0.50) - 0.60) < 0.001
def test_ev_negative(): assert abs(compute_ev_score(0.30, 0.50) - (-0.40)) < 0.001
def test_ev_zero(): assert abs(compute_ev_score(0.50, 0.50)) < 0.001
def test_ev_none_extreme(): assert compute_ev_score(0.50, 0.0) is None and compute_ev_score(0.50, 1.0) is None

def _pred(**kw): return Prediction(raw_post_id="twitter:1", category="crypto", predicted_outcome="Yes", extracted_at=NOW, **kw)
def _src(**kw):
    d = dict(handle="t", platform="twitter", global_credibility=0.6, qualifying_predictions=15, accuracy_unlocked=True, verified=True, follower_count=10000, engagement_ratio=0.05)
    d.update(kw)
    s = Source(**d); s.categories_predicted_in = kw.get("_c", ["crypto", "politics", "sports"]); s.category_credibility = kw.get("_cc", {"crypto": 0.6})
    return s

def test_risk_unrated(): f, r = compute_risk_flags(_pred(), _src(accuracy_unlocked=False)); assert f and "insufficient history" in r[0].lower()
def test_risk_low_cred(): f, r = compute_risk_flags(_pred(), _src(global_credibility=0.2)); assert f and any("Low global" in x for x in r)
def test_risk_extreme_market(): f, r = compute_risk_flags(_pred(market_implied_probability=0.03), _src()); assert f
def test_risk_negative_ev(): f, r = compute_risk_flags(_pred(ev_score=-0.5), _src()); assert f
def test_risk_too_close(): f, r = compute_risk_flags(_pred(hours_remaining_at_prediction=6.0), _src()); assert f
def test_risk_insufficient(): f, r = compute_risk_flags(_pred(), _src(qualifying_predictions=5)); assert f
def test_risk_specialised(): f, r = compute_risk_flags(_pred(), _src(_c=["crypto"])); assert f
def test_risk_untrusted(): f, r = compute_risk_flags(_pred(), _src(trusted=False)); assert f
def test_risk_no_source(): f, r = compute_risk_flags(_pred(), None); assert f
def test_no_risk_clean(): f, r = compute_risk_flags(_pred(ev_score=0.5, market_implied_probability=0.50, hours_remaining_at_prediction=48.0), _src()); assert not f

"""Per-model tests for Tier A structure models (all decomposition/detectors).

Each: fit on a tiny synthetic series, assert predict() raises NotImplementedError,
that transform()/reconstruct() have sensible shapes, and that state() is populated.
"""

import numpy as np
import pytest

from models.tier_a_structure import (
    CorrelationChangeDetector,
    GraphLaplacian,
    NMFAbsReturns,
    PCAReturns,
    RMTFilter,
    TruncatedSVDReturns,
)

TIER_A = [
    lambda: PCAReturns(),
    lambda: RMTFilter(),
    lambda: TruncatedSVDReturns(),
    lambda: CorrelationChangeDetector(window=40),
    lambda: GraphLaplacian(),
    lambda: NMFAbsReturns(k=3),
]


@pytest.mark.parametrize("factory", TIER_A)
def test_predict_raises(factory, tiny_md):
    m = factory().fit(tiny_md)
    with pytest.raises(NotImplementedError):
        m.predict(1)
    assert m.target is None


@pytest.mark.parametrize("factory", TIER_A)
def test_state_populated(factory, tiny_md):
    m = factory().fit(tiny_md)
    st = m.state()
    assert isinstance(st, dict) and len(st) > 0
    assert all(v is not None for v in st.values())


def test_pca_reconstruct_and_factors(tiny_md):
    m = PCAReturns(threshold=0.9).fit(tiny_md)
    assert m.transform().shape[0] == tiny_md.R.shape[0]
    rec = m.reconstruct(k=tiny_md.n)  # full rank -> near-perfect reconstruction
    assert np.allclose(rec, tiny_md.R, atol=1e-8)
    assert 1 <= m.n_factors() <= tiny_md.n
    assert 0 < m.state()["market_factor_share"] <= 1


def test_rmt_separates_signal(tiny_md):
    m = RMTFilter().fit(tiny_md)
    st = m.state()
    assert st["n_signal_eigenvalues"] >= 1  # the market factor is signal
    C = st["cleaned_correlation"]
    assert np.allclose(np.diag(C), 1.0)
    assert np.allclose(C, C.T)


def test_svd_low_rank_error_decreases(tiny_md):
    m = TruncatedSVDReturns().fit(tiny_md)
    e1 = np.linalg.norm(m.reconstruct(1) - tiny_md.R)
    e3 = np.linalg.norm(m.reconstruct(3) - tiny_md.R)
    assert e3 <= e1


def test_change_detector_flags_breaks(tiny_md):
    m = CorrelationChangeDetector(window=40, z_threshold=2.5).fit(tiny_md)
    d = m.detect()
    assert d["distance_series"].ndim == 1
    assert m.state()["n_breaks"] >= 0


def test_graph_laplacian_fiedler(tiny_md):
    m = GraphLaplacian().fit(tiny_md)
    st = m.state()
    assert st["fiedler_value"] >= -1e-9
    assert m.transform(2).shape == (tiny_md.n, 2)
    assert len(st["cluster_labels"]) == tiny_md.n


def test_nmf_nonnegative(tiny_md):
    m = NMFAbsReturns(k=3, max_iter=200).fit(tiny_md)
    assert np.all(m._W >= 0) and np.all(m._H >= 0)
    assert m.reconstruct().shape == tiny_md.R.shape
    assert len(m.cluster_membership()) == tiny_md.n

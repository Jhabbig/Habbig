"""Unit tests for the confidence-score helper in spike_detector.

Tests the pure function directly — no DB, no async. Three required
cases from the polish-layer spec plus a few boundary checks.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from spike_detector import _compute_confidence  # noqa: E402


class TestConfidence:
    def test_high_z_high_mult_returns_high(self):
        # z=10, mult=10, bt=0.5 → z_c=50, m_c=25, bt_c=12.5 → 87.5
        c = _compute_confidence(z=10, multiple=10, backtest_hit_rate=0.5)
        assert c >= 70, f"strong spike should land in the 'green' tier, got {c}"

    def test_warmup_returns_flat_30(self):
        # Warmup bypasses the components entirely — flat low-medium.
        c = _compute_confidence(z=0, multiple=0, warmup=True)
        assert c == 30.0

    def test_bounded_0_to_100(self):
        # Random-ish inputs across the plausible range should stay in [0, 100].
        for z in (-5, 0, 3, 5, 10, 50):
            for mult in (-5, 0, 3, 5, 10, 50):
                for bt in (-0.5, 0.0, 0.3, 0.5, 0.8, 1.0, 1.5):
                    c = _compute_confidence(z=z, multiple=mult, backtest_hit_rate=bt)
                    assert 0.0 <= c <= 100.0, f"out of bounds at z={z} m={mult} bt={bt}: {c}"

    # ── Boundary / tier checks (extras) ──

    def test_at_gate_threshold_is_low(self):
        # z=3, mult=3 sits exactly at the gate threshold — components
        # evaluate to zero. With backtest=0.5 (neutral) → 12.5. That's
        # the "red" tier — we've just barely passed the gate and have
        # no history to trust yet.
        c = _compute_confidence(z=3, multiple=3, backtest_hit_rate=0.5)
        assert 10 <= c <= 15

    def test_zero_backtest_penalises(self):
        # bt=0 means historical track record is zero → maximum penalty.
        c = _compute_confidence(z=10, multiple=10, backtest_hit_rate=0.0)
        # Same z+mult as "high" test but without the backtest bump
        assert c < 80

    def test_negative_z_clamps_to_zero(self):
        # z component is max(0, ...) so negative z should give 0 from z.
        c = _compute_confidence(z=-10, multiple=10, backtest_hit_rate=0.5)
        # 0 (z) + 25 (mult) + 12.5 (bt) = 37.5
        assert 35 <= c <= 40

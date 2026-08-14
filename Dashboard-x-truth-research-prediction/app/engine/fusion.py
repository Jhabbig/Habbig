"""Swappable fusion strategies + probability calibrators.

Fusion v0 is a transparent weighted ensemble — a configurable linear or
logistic combination of normalized signals — followed by an optional Platt or
isotonic calibration step. Everything is behind the ``FusionStrategy``
interface so a trained model can replace it later without touching ingestion
or serving: register it in ``_REGISTRY`` and flip ``engine.fusion.strategy``
in config.yaml.

⚠ Legal constraint: the calibrator fitting helpers (``fit_platt``,
``fit_isotonic``) accept only (raw_score, realized_outcome) pairs — i.e. our
own logged predictions graded against ground truth. Never feed X/Reddit
content into them; the platforms' terms prohibit training on their API data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Protocol, Sequence, Tuple

FUSION_VERSION = "weighted_v0.1"

_EPS = 1e-6


@dataclass
class SignalReading:
    name: str
    value: float   # normalized to [0, 1]
    weight: float  # configured (pre-normalization) weight


@dataclass
class FusionResult:
    p_yes: float
    raw_score: float       # pre-calibration probability
    agreement: float       # 1 - weighted dispersion of the signals, in [0, 1]
    signals: List[SignalReading]  # with weights renormalized over those present


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _logit(p: float) -> float:
    p = min(max(p, _EPS), 1.0 - _EPS)
    return math.log(p / (1.0 - p))


# ---------------------------------------------------------------------------
# Calibrators
# ---------------------------------------------------------------------------
class IdentityCalibrator:
    name = "none"

    def apply(self, p: float) -> float:
        return p


class PlattCalibrator:
    """Sigmoid recalibration in logit space: p' = sigmoid(a * logit(p) + b)."""
    name = "platt"

    def __init__(self, a: float = 1.0, b: float = 0.0) -> None:
        self.a = a
        self.b = b

    def apply(self, p: float) -> float:
        return _sigmoid(self.a * _logit(p) + self.b)


class IsotonicCalibrator:
    """Piecewise-linear interpolation over monotone (raw, calibrated) points."""
    name = "isotonic"

    def __init__(self, points: Sequence[Tuple[float, float]]) -> None:
        self.points = sorted((float(x), float(y)) for x, y in points)

    def apply(self, p: float) -> float:
        pts = self.points
        if not pts:
            return p
        if p <= pts[0][0]:
            return pts[0][1]
        if p >= pts[-1][0]:
            return pts[-1][1]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if x0 <= p <= x1:
                if x1 == x0:
                    return y1
                t = (p - x0) / (x1 - x0)
                return y0 + t * (y1 - y0)
        return p


def build_calibrator(calibration_cfg: dict):
    method = (calibration_cfg or {}).get("method", "none")
    if method == "platt":
        params = calibration_cfg.get("platt", {}) or {}
        return PlattCalibrator(a=float(params.get("a", 1.0)), b=float(params.get("b", 0.0)))
    if method == "isotonic":
        points = calibration_cfg.get("isotonic_points", []) or []
        return IsotonicCalibrator([(p[0], p[1]) for p in points if len(p) == 2])
    return IdentityCalibrator()


# ---------------------------------------------------------------------------
# Calibrator fitting — train ONLY on logged (raw_score, realized_outcome) pairs
# ---------------------------------------------------------------------------
def fit_platt(pairs: Sequence[Tuple[float, bool]], iterations: int = 200, lr: float = 0.5) -> Tuple[float, float]:
    """Fit (a, b) by gradient descent on logistic loss over logit(raw_score).

    ``pairs`` are (raw fused score, realized outcome) from the fusion_audit
    table — our own predictions vs ground truth, nothing else.
    """
    if not pairs:
        return 1.0, 0.0
    xs = [_logit(p) for p, _ in pairs]
    ys = [1.0 if y else 0.0 for _, y in pairs]
    a, b = 1.0, 0.0
    n = len(xs)
    for _ in range(iterations):
        grad_a = grad_b = 0.0
        for x, y in zip(xs, ys):
            err = _sigmoid(a * x + b) - y
            grad_a += err * x
            grad_b += err
        a -= lr * grad_a / n
        b -= lr * grad_b / n
    return a, b


def fit_isotonic(pairs: Sequence[Tuple[float, bool]]) -> List[Tuple[float, float]]:
    """Pool-adjacent-violators regression → monotone (raw, calibrated) points."""
    if not pairs:
        return []
    data = sorted((float(p), 1.0 if y else 0.0) for p, y in pairs)
    # blocks of (x_mean, y_mean, weight)
    blocks: List[List[float]] = [[x, y, 1.0] for x, y in data]
    merged: List[List[float]] = []
    for block in blocks:
        merged.append(block)
        while len(merged) >= 2 and merged[-2][1] > merged[-1][1]:
            x1, y1, w1 = merged[-2]
            x2, y2, w2 = merged[-1]
            merged[-2:] = [[(x1 * w1 + x2 * w2) / (w1 + w2), (y1 * w1 + y2 * w2) / (w1 + w2), w1 + w2]]
    return [(x, y) for x, y, _ in merged]


# ---------------------------------------------------------------------------
# Fusion strategies
# ---------------------------------------------------------------------------
class FusionStrategy(Protocol):
    name: str

    def fuse(self, signals: List[SignalReading]) -> FusionResult: ...


class WeightedEnsembleFusion:
    """Fusion v0 — configurable linear/logistic combination of normalized signals."""
    name = "weighted_v0"

    def __init__(self, combination: str = "logistic", logistic_scale: float = 4.0,
                 logistic_bias: float = 0.0, calibrator=None) -> None:
        self.combination = combination
        self.logistic_scale = logistic_scale
        self.logistic_bias = logistic_bias
        self.calibrator = calibrator or IdentityCalibrator()

    def fuse(self, signals: List[SignalReading]) -> FusionResult:
        present = [s for s in signals if s.weight > 0.0]
        if not present:
            # no information — the uninformative prior, zero agreement
            return FusionResult(p_yes=0.5, raw_score=0.5, agreement=0.0, signals=[])

        total_weight = sum(s.weight for s in present)
        normalized = [SignalReading(s.name, s.value, s.weight / total_weight) for s in present]

        mean = sum(s.weight * s.value for s in normalized)
        if self.combination == "linear":
            raw = mean
        else:  # logistic — 0.5-neutral push into a sigmoid
            z = self.logistic_bias + self.logistic_scale * (mean - 0.5)
            raw = _sigmoid(z)

        p = min(max(self.calibrator.apply(raw), 0.0), 1.0)

        # agreement: 1 - 2 * weighted std of signal values (std maxes at 0.5 on [0,1])
        variance = sum(s.weight * (s.value - mean) ** 2 for s in normalized)
        agreement = max(0.0, 1.0 - 2.0 * math.sqrt(variance))

        return FusionResult(p_yes=p, raw_score=raw, agreement=agreement, signals=normalized)


_REGISTRY: Dict[str, Callable[[dict], FusionStrategy]] = {
    "weighted_v0": lambda cfg: WeightedEnsembleFusion(
        combination=cfg["fusion"].get("combination", "logistic"),
        logistic_scale=float(cfg["fusion"].get("logistic_scale", 4.0)),
        logistic_bias=float(cfg["fusion"].get("logistic_bias", 0.0)),
        calibrator=build_calibrator(cfg["fusion"].get("calibration", {})),
    ),
}


def build_fusion(cfg: dict) -> FusionStrategy:
    """Instantiate the configured strategy; unknown names fall back to v0."""
    strategy = cfg["fusion"].get("strategy", "weighted_v0")
    factory = _REGISTRY.get(strategy) or _REGISTRY["weighted_v0"]
    return factory(cfg)


def register_fusion(name: str, factory: Callable[[dict], FusionStrategy]) -> None:
    """Plug in a replacement fusion (e.g. a trained model) without touching serving."""
    _REGISTRY[name] = factory

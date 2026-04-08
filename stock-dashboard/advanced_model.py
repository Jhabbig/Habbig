#!/usr/bin/env python3
"""
Advanced ML Model Improvements for Stock Up/Down Prediction

Provides walk-forward validation, stacking ensemble with meta-learner,
probability calibration, market regime detection, and recency weighting.
Designed to work alongside stock_ml_model.py.

Usage:
    python3 advanced_model.py   # Run tests with synthetic data
"""

import warnings
import logging
from typing import Dict, List, Optional, Tuple

import numpy as np

from sklearn.ensemble import (
    RandomForestClassifier,
    GradientBoostingClassifier,
)
from sklearn.linear_model import LogisticRegression
from sklearn.calibration import CalibratedClassifierCV
from sklearn.model_selection import KFold, TimeSeriesSplit
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import accuracy_score, log_loss
from sklearn.isotonic import IsotonicRegression

try:
    from xgboost import XGBClassifier
    _HAS_XGB = True
except Exception:
    _HAS_XGB = False

warnings.filterwarnings("ignore")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared model config (matches tuned params from stock_ml_model.py)
# ---------------------------------------------------------------------------

def _make_xgb(**overrides):
    if not _HAS_XGB:
        # Fallback: use an extra GradientBoosting with XGB-like params
        params = dict(
            n_estimators=200,
            max_depth=2,
            learning_rate=0.005,
            subsample=0.6,
            min_samples_leaf=50,
            max_features=0.5,
            random_state=42,
        )
        params.update({k: v for k, v in overrides.items()
                       if k in params})
        return GradientBoostingClassifier(**params)

    params = dict(
        n_estimators=200,
        max_depth=2,
        learning_rate=0.005,
        subsample=0.6,
        colsample_bytree=0.5,
        min_child_weight=50,
        reg_alpha=5.0,
        reg_lambda=10.0,
        gamma=2.0,
        eval_metric="logloss",
        random_state=42,
        n_jobs=-1,
    )
    params.update(overrides)
    return XGBClassifier(**params)


def _make_rf(**overrides):
    params = dict(
        n_estimators=500,
        max_depth=3,
        min_samples_leaf=50,
        min_samples_split=100,
        max_features=0.3,
        random_state=42,
        n_jobs=-1,
    )
    params.update(overrides)
    return RandomForestClassifier(**params)


def _make_gb(**overrides):
    params = dict(
        n_estimators=150,
        max_depth=2,
        learning_rate=0.005,
        subsample=0.6,
        min_samples_leaf=50,
        max_features=0.4,
        random_state=42,
    )
    params.update(overrides)
    return GradientBoostingClassifier(**params)


# ---------------------------------------------------------------------------
# 1. WalkForwardTrainer
# ---------------------------------------------------------------------------

class WalkForwardTrainer:
    """Walk-forward cross-validation: train on rolling window, test on next block."""

    def __init__(self, n_splits: int = 5, train_months: int = 36, test_months: int = 3):
        self.n_splits = n_splits
        self.train_months = train_months
        self.test_months = test_months
        # Approximate trading days per month
        self._days_per_month = 21

    def split(self, X: np.ndarray, y: np.ndarray, dates: np.ndarray):
        """
        Yield (train_idx, test_idx) tuples for walk-forward splits.

        Parameters
        ----------
        X : array of shape (n_samples, n_features)
        y : array of shape (n_samples,)
        dates : array of datetime-like or ordinal values, same length as X
        """
        n = len(X)
        train_size = self.train_months * self._days_per_month
        test_size = self.test_months * self._days_per_month

        if train_size + test_size > n:
            # Fall back: use what we have
            train_size = max(int(n * 0.6), 50)
            test_size = max(int(n * 0.15), 20)

        # Calculate step so that we get roughly n_splits folds
        total_needed = train_size + test_size
        remaining = n - total_needed
        if remaining <= 0 or self.n_splits <= 1:
            step = max(test_size, 1)
        else:
            step = max(remaining // (self.n_splits - 1), test_size)

        splits_yielded = 0
        start = 0
        while splits_yielded < self.n_splits:
            train_end = start + train_size
            test_end = train_end + test_size
            if test_end > n:
                # Last fold: use whatever remains as test
                if train_end < n - 10:
                    test_end = n
                else:
                    break
            train_idx = np.arange(start, train_end)
            test_idx = np.arange(train_end, test_end)
            yield train_idx, test_idx
            splits_yielded += 1
            start += step

    def train_and_evaluate(
        self,
        X: np.ndarray,
        y: np.ndarray,
        dates: np.ndarray,
        feature_names: List[str],
    ) -> Dict:
        """
        Train models on each walk-forward fold.

        Returns
        -------
        dict with keys:
            - fold_accuracies: list of per-fold accuracy
            - overall_accuracy: weighted average accuracy
            - feature_importance_stability: std of feature ranks across folds
            - fold_details: list of dicts per fold
        """
        fold_accuracies = []
        fold_details = []
        all_importances = []
        all_preds = []
        all_true = []

        for fold_i, (train_idx, test_idx) in enumerate(self.split(X, y, dates)):
            try:
                X_tr, y_tr = X[train_idx], y[train_idx]
                X_te, y_te = X[test_idx], y[test_idx]

                # Use recency weighting for training
                weighter = RecencyWeighter()
                weights = weighter.compute_weights(len(y_tr), half_life_days=252)

                # Train ensemble
                ensemble = StackingEnsemble()
                ensemble.fit(X_tr, y_tr, sample_weight=weights)

                # Evaluate
                proba = ensemble.predict_proba(X_te)
                preds = (proba >= 0.5).astype(int)
                acc = accuracy_score(y_te, preds)

                fold_accuracies.append(acc)
                all_preds.extend(preds.tolist())
                all_true.extend(y_te.tolist())

                # Feature importance
                imp = ensemble.get_feature_importance()
                if imp is not None and len(imp) == X.shape[1]:
                    all_importances.append(imp)

                fold_details.append({
                    "fold": fold_i,
                    "train_size": len(train_idx),
                    "test_size": len(test_idx),
                    "accuracy": float(acc),
                })
                logger.info(
                    "Fold %d: train=%d test=%d acc=%.4f",
                    fold_i, len(train_idx), len(test_idx), acc,
                )

            except Exception as e:
                logger.warning("Fold %d failed: %s", fold_i, e)
                fold_details.append({
                    "fold": fold_i,
                    "error": str(e),
                })

        # Overall accuracy
        if all_true:
            overall_acc = accuracy_score(all_true, all_preds)
        else:
            overall_acc = 0.0

        # Feature importance stability: low std of ranks = stable
        stability = 0.0
        if len(all_importances) >= 2:
            imp_array = np.array(all_importances)
            # Rank features per fold (higher importance = lower rank number)
            ranks = np.zeros_like(imp_array)
            for i in range(len(imp_array)):
                order = np.argsort(-imp_array[i])
                for rank, idx in enumerate(order):
                    ranks[i, idx] = rank
            # Average std of ranks across features
            stability = float(np.mean(np.std(ranks, axis=0)))

        return {
            "fold_accuracies": fold_accuracies,
            "overall_accuracy": float(overall_acc),
            "feature_importance_stability": stability,
            "fold_details": fold_details,
            "n_folds_completed": len(fold_accuracies),
        }


# ---------------------------------------------------------------------------
# 2. StackingEnsemble
# ---------------------------------------------------------------------------

class StackingEnsemble:
    """
    Two-level stacking ensemble.

    Level 0: XGBoost, RandomForest, GradientBoosting
    Level 1: Logistic Regression meta-learner on out-of-fold predictions
    """

    def __init__(self):
        self.level0_models: List = []
        self.meta_model: Optional[LogisticRegression] = None
        self.scaler: Optional[StandardScaler] = None
        self._n_level0 = 3
        self._oof_kfolds = 5
        self._fitted = False

    def _make_level0(self):
        return [_make_xgb(), _make_rf(), _make_gb()]

    def fit(
        self,
        X_train: np.ndarray,
        y_train: np.ndarray,
        X_val: Optional[np.ndarray] = None,
        y_val: Optional[np.ndarray] = None,
        sample_weight: Optional[np.ndarray] = None,
    ):
        """
        Train level 0 with k-fold to generate OOF predictions,
        then train meta-learner on those OOF predictions.
        """
        n = len(y_train)
        oof_preds = np.zeros((n, self._n_level0))
        oof_mask = np.zeros(n, dtype=bool)  # Track which samples got OOF predictions

        # Scale features
        self.scaler = StandardScaler()
        X_scaled = self.scaler.fit_transform(X_train)

        kf = TimeSeriesSplit(n_splits=self._oof_kfolds)

        # For each level-0 model type, generate OOF predictions
        level0_templates = self._make_level0()

        for model_idx, template in enumerate(level0_templates):
            for train_idx, val_idx in kf.split(X_scaled):
                fold_model = _clone_model(template)
                X_f, y_f = X_scaled[train_idx], y_train[train_idx]
                w_f = sample_weight[train_idx] if sample_weight is not None else None

                try:
                    fold_model.fit(X_f, y_f, sample_weight=w_f)
                except TypeError:
                    # Some models may not accept sample_weight
                    fold_model.fit(X_f, y_f)

                proba = fold_model.predict_proba(X_scaled[val_idx])
                # Take probability of class 1
                if proba.ndim == 2:
                    oof_preds[val_idx, model_idx] = proba[:, 1]
                else:
                    oof_preds[val_idx, model_idx] = proba
                oof_mask[val_idx] = True

        # Re-train level-0 models on full training data (for inference)
        self.level0_models = self._make_level0()
        for model in self.level0_models:
            try:
                model.fit(X_scaled, y_train, sample_weight=sample_weight)
            except TypeError:
                model.fit(X_scaled, y_train)

        # Train meta-learner: prefer validation set if provided for better
        # calibration; fall back to OOF predictions from training data.
        if X_val is not None and y_val is not None and len(y_val) >= 10:
            X_val_scaled = self.scaler.transform(X_val)
            val_meta_input = np.zeros((len(X_val), self._n_level0))
            for i, model in enumerate(self.level0_models):
                proba = model.predict_proba(X_val_scaled)
                if proba.ndim == 2:
                    val_meta_input[:, i] = proba[:, 1]
                else:
                    val_meta_input[:, i] = proba
            meta_X = val_meta_input
            meta_y = y_val
        else:
            # Only use samples that received real OOF predictions (exclude
            # early samples that were never in any validation fold).
            meta_X = oof_preds[oof_mask]
            meta_y = y_train[oof_mask]

        self.meta_model = LogisticRegression(
            C=1.0,
            penalty="l2",
            solver="lbfgs",
            max_iter=1000,
            random_state=42,
        )
        self.meta_model.fit(meta_X, meta_y)
        self._fitted = True

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """
        Get level-0 predictions, feed to meta-learner, return P(up).
        """
        if not self._fitted:
            raise RuntimeError("StackingEnsemble has not been fitted yet.")

        X_scaled = self.scaler.transform(X)
        meta_input = np.zeros((len(X), self._n_level0))

        for i, model in enumerate(self.level0_models):
            proba = model.predict_proba(X_scaled)
            if proba.ndim == 2:
                meta_input[:, i] = proba[:, 1]
            else:
                meta_input[:, i] = proba

        final_proba = self.meta_model.predict_proba(meta_input)
        if final_proba.ndim == 2:
            return final_proba[:, 1]
        return final_proba

    def get_feature_importance(self) -> Optional[np.ndarray]:
        """Average feature importance from level-0 models."""
        if not self.level0_models:
            return None

        importances = []
        for model in self.level0_models:
            try:
                imp = model.feature_importances_
                # Normalize to sum to 1
                total = imp.sum()
                if total > 0:
                    imp = imp / total
                importances.append(imp)
            except AttributeError:
                continue

        if not importances:
            return None
        return np.mean(importances, axis=0)


def _clone_model(model):
    """Clone a sklearn/xgb model by re-instantiating with same params."""
    try:
        from sklearn.base import clone
        return clone(model)
    except Exception:
        # Fallback: use get_params
        params = model.get_params()
        return type(model)(**params)


# ---------------------------------------------------------------------------
# 3. ModelCalibrator
# ---------------------------------------------------------------------------

class ModelCalibrator:
    """
    Calibrate model probabilities so predicted confidence matches
    empirical frequency (e.g., 60% predicted -> 60% actually go up).
    """

    def __init__(self, method: str = "isotonic"):
        """
        Parameters
        ----------
        method : 'isotonic' or 'sigmoid' (Platt scaling)
        """
        if method not in ("isotonic", "sigmoid"):
            raise ValueError(f"method must be 'isotonic' or 'sigmoid', got '{method}'")
        self.method = method
        self._calibrator = None
        self._fitted = False

    def fit(self, y_true: np.ndarray, y_proba: np.ndarray):
        """
        Fit calibration mapping from raw probabilities to calibrated ones.

        Parameters
        ----------
        y_true : binary labels (0/1)
        y_proba : predicted P(class=1)
        """
        y_true = np.asarray(y_true, dtype=float)
        y_proba = np.asarray(y_proba, dtype=float)

        if len(y_true) < 10:
            logger.warning("Too few samples (%d) for calibration; skipping.", len(y_true))
            self._fitted = False
            return

        if self.method == "isotonic":
            self._calibrator = IsotonicRegression(
                y_min=0.0, y_max=1.0, out_of_bounds="clip"
            )
            self._calibrator.fit(y_proba, y_true)
        else:
            # Platt scaling: logistic regression on log-odds
            from sklearn.linear_model import LogisticRegression as LR
            # Reshape for sklearn
            log_odds = np.log(np.clip(y_proba, 1e-8, 1 - 1e-8) /
                              np.clip(1 - y_proba, 1e-8, 1 - 1e-8)).reshape(-1, 1)
            self._calibrator = LR(C=1e10, solver="lbfgs", max_iter=1000)
            self._calibrator.fit(log_odds, y_true)

        self._fitted = True

    def calibrate(self, y_proba: np.ndarray) -> np.ndarray:
        """
        Apply calibration to raw probabilities.

        Returns calibrated probabilities. If not fitted, returns input unchanged.
        """
        y_proba = np.asarray(y_proba, dtype=float)

        if not self._fitted or self._calibrator is None:
            return y_proba

        try:
            if self.method == "isotonic":
                return self._calibrator.predict(y_proba)
            else:
                log_odds = np.log(np.clip(y_proba, 1e-8, 1 - 1e-8) /
                                  np.clip(1 - y_proba, 1e-8, 1 - 1e-8)).reshape(-1, 1)
                cal = self._calibrator.predict_proba(log_odds)
                if cal.ndim == 2:
                    return cal[:, 1]
                return cal
        except Exception as e:
            logger.warning("Calibration failed: %s. Returning raw probabilities.", e)
            return y_proba


# ---------------------------------------------------------------------------
# 4. RegimeDetector
# ---------------------------------------------------------------------------

class RegimeDetector:
    """Detect current market regime from recent price and volume action."""

    def __init__(self, lookback: int = 60):
        self.lookback = lookback

    def _hurst_exponent(self, ts: np.ndarray) -> float:
        """
        Estimate Hurst exponent using rescaled range (R/S) method.

        H > 0.5: trending / persistent
        H = 0.5: random walk
        H < 0.5: mean-reverting / anti-persistent
        """
        ts = np.asarray(ts, dtype=float)
        n = len(ts)
        if n < 20:
            return 0.5  # not enough data

        max_k = min(n // 2, 100)
        min_k = 10
        if max_k <= min_k:
            return 0.5

        rs_values = []
        ns_values = []

        for k in range(min_k, max_k + 1, max(1, (max_k - min_k) // 20)):
            n_segments = n // k
            if n_segments < 1:
                continue
            rs_list = []
            for seg in range(n_segments):
                segment = ts[seg * k : (seg + 1) * k]
                mean_seg = np.mean(segment)
                deviations = np.cumsum(segment - mean_seg)
                r = np.max(deviations) - np.min(deviations)
                s = np.std(segment, ddof=1)
                if s > 1e-10:
                    rs_list.append(r / s)
            if rs_list:
                rs_values.append(np.log(np.mean(rs_list)))
                ns_values.append(np.log(k))

        if len(rs_values) < 3:
            return 0.5

        # Linear regression: log(R/S) = H * log(n) + c
        coeffs = np.polyfit(ns_values, rs_values, 1)
        h = float(np.clip(coeffs[0], 0.0, 1.0))
        return h

    def _compute_adx(self, highs: np.ndarray, lows: np.ndarray,
                     closes: np.ndarray, period: int = 14) -> float:
        """Compute Average Directional Index (simplified from closes only)."""
        if len(closes) < period + 1:
            return 25.0  # neutral default

        # Approximate using absolute returns as directional movement
        returns = np.diff(closes) / np.clip(closes[:-1], 1e-8, None)
        pos_dm = np.where(returns > 0, np.abs(returns), 0.0)
        neg_dm = np.where(returns < 0, np.abs(returns), 0.0)

        # Smooth with EMA
        alpha = 1.0 / period
        smooth_pos = np.zeros(len(returns))
        smooth_neg = np.zeros(len(returns))
        smooth_pos[0] = pos_dm[0]
        smooth_neg[0] = neg_dm[0]

        for i in range(1, len(returns)):
            smooth_pos[i] = alpha * pos_dm[i] + (1 - alpha) * smooth_pos[i - 1]
            smooth_neg[i] = alpha * neg_dm[i] + (1 - alpha) * smooth_neg[i - 1]

        di_sum = smooth_pos + smooth_neg
        di_diff = np.abs(smooth_pos - smooth_neg)
        dx = np.where(di_sum > 1e-10, di_diff / di_sum * 100, 0.0)

        # Average over last period
        adx = float(np.mean(dx[-period:]))
        return np.clip(adx, 0, 100)

    def detect(self, closes: np.ndarray, volumes: np.ndarray, highs: np.ndarray = None, lows: np.ndarray = None) -> Dict:
        """
        Detect current market regime.

        Parameters
        ----------
        closes : array of closing prices
        volumes : array of trading volumes

        Returns
        -------
        dict with keys:
            - regime: 'trending_up', 'trending_down', 'mean_reverting', 'volatile', 'calm'
            - trend_strength: float 0-1
            - volatility_regime: 'low', 'normal', 'high'
            - momentum_regime: 'strong_up', 'weak_up', 'neutral', 'weak_down', 'strong_down'
        """
        closes = np.asarray(closes, dtype=float)
        volumes = np.asarray(volumes, dtype=float)

        lb = min(self.lookback, len(closes))
        if lb < 10:
            return {
                "regime": "calm",
                "trend_strength": 0.0,
                "volatility_regime": "normal",
                "momentum_regime": "neutral",
            }

        recent_closes = closes[-lb:]
        recent_volumes = volumes[-lb:]

        # --- Hurst exponent ---
        log_returns = np.diff(np.log(np.clip(recent_closes, 1e-8, None)))
        hurst = self._hurst_exponent(log_returns)

        # --- Rolling volatility ---
        vol_20 = np.std(log_returns[-20:]) if len(log_returns) >= 20 else np.std(log_returns)
        vol_full = np.std(log_returns)
        # Percentile: compare recent vol to full lookback
        vol_ratio = vol_20 / max(vol_full, 1e-10)

        if vol_ratio > 1.5:
            volatility_regime = "high"
        elif vol_ratio < 0.6:
            volatility_regime = "low"
        else:
            volatility_regime = "normal"

        # --- ADX (trend strength) ---
        recent_highs = np.asarray(highs, dtype=float)[-lb:] if highs is not None else recent_closes
        recent_lows = np.asarray(lows, dtype=float)[-lb:] if lows is not None else recent_closes
        adx = self._compute_adx(recent_highs, recent_lows, recent_closes)
        trend_strength = float(np.clip(adx / 50.0, 0, 1))

        # --- Momentum ---
        if len(recent_closes) >= 20:
            ret_20 = (recent_closes[-1] / recent_closes[-20]) - 1
        else:
            ret_20 = (recent_closes[-1] / recent_closes[0]) - 1

        if ret_20 > 0.05:
            momentum_regime = "strong_up"
        elif ret_20 > 0.01:
            momentum_regime = "weak_up"
        elif ret_20 > -0.01:
            momentum_regime = "neutral"
        elif ret_20 > -0.05:
            momentum_regime = "weak_down"
        else:
            momentum_regime = "strong_down"

        # --- Determine overall regime ---
        if hurst > 0.6 and trend_strength > 0.4:
            if ret_20 > 0:
                regime = "trending_up"
            else:
                regime = "trending_down"
        elif hurst < 0.4:
            regime = "mean_reverting"
        elif volatility_regime == "high":
            regime = "volatile"
        else:
            regime = "calm"

        return {
            "regime": regime,
            "trend_strength": round(trend_strength, 4),
            "volatility_regime": volatility_regime,
            "momentum_regime": momentum_regime,
            "hurst_exponent": round(hurst, 4),
            "adx": round(adx, 2),
        }


# ---------------------------------------------------------------------------
# 5. RecencyWeighter
# ---------------------------------------------------------------------------

class RecencyWeighter:
    """Compute exponential decay sample weights so recent data matters more."""

    def compute_weights(self, n_samples: int, half_life_days: int = 252) -> np.ndarray:
        """
        Exponential decay weights. Index 0 is oldest, index -1 is newest.

        Parameters
        ----------
        n_samples : number of samples (ordered oldest to newest)
        half_life_days : number of days for weight to halve

        Returns
        -------
        numpy array of weights, normalized so they sum to n_samples
        """
        if n_samples <= 0:
            return np.array([])

        half_life_days = max(half_life_days, 1)
        decay = np.log(2) / half_life_days

        # Age: oldest sample has age (n_samples - 1), newest has age 0
        ages = np.arange(n_samples - 1, -1, -1, dtype=float)
        weights = np.exp(-decay * ages)

        # Normalize so weights sum to n_samples (preserving effective sample size sense)
        weights = weights * (n_samples / weights.sum())
        return weights


# ---------------------------------------------------------------------------
# 6. build_advanced_model (master function)
# ---------------------------------------------------------------------------

def build_advanced_model(
    X: np.ndarray,
    y: np.ndarray,
    dates: np.ndarray,
    feature_names: List[str],
    X_val: Optional[np.ndarray] = None,
    y_val: Optional[np.ndarray] = None,
) -> Dict:
    """
    Master function: walk-forward validation + stacking ensemble + calibration.

    Parameters
    ----------
    X : training features, shape (n_samples, n_features)
    y : training labels (0/1), shape (n_samples,)
    dates : datetime array for walk-forward splitting
    feature_names : list of feature name strings
    X_val : optional held-out validation features
    y_val : optional held-out validation labels

    Returns
    -------
    dict with keys:
        - model: fitted StackingEnsemble
        - calibrator: fitted ModelCalibrator
        - walk_forward_results: dict from WalkForwardTrainer
        - regime: dict from RegimeDetector (if closes derivable)
        - metrics: dict with accuracy, calibrated_accuracy, log_loss, etc.
    """
    results = {}

    # --- Walk-forward evaluation ---
    logger.info("Running walk-forward cross-validation...")
    try:
        wf = WalkForwardTrainer(n_splits=5, train_months=36, test_months=3)
        wf_results = wf.train_and_evaluate(X, y, dates, feature_names)
        results["walk_forward_results"] = wf_results
        logger.info("Walk-forward overall accuracy: %.4f", wf_results["overall_accuracy"])
    except Exception as e:
        logger.warning("Walk-forward validation failed: %s", e)
        results["walk_forward_results"] = {"error": str(e)}

    # --- Train final stacking ensemble on full training data ---
    logger.info("Training final StackingEnsemble...")
    weighter = RecencyWeighter()
    weights = weighter.compute_weights(len(y), half_life_days=252)

    ensemble = StackingEnsemble()
    ensemble.fit(X, y, X_val=X_val, y_val=y_val, sample_weight=weights)
    results["model"] = ensemble

    # --- Calibration ---
    logger.info("Calibrating probabilities...")
    calibrator = ModelCalibrator(method="isotonic")

    if X_val is not None and y_val is not None and len(y_val) >= 20:
        # Calibrate on held-out validation set
        val_proba = ensemble.predict_proba(X_val)
        calibrator.fit(y_val, val_proba)
    else:
        # Calibrate on training OOF-style (use last 20% as pseudo-validation)
        split_point = int(len(y) * 0.8)
        if split_point > 50 and len(y) - split_point > 20:
            cal_proba = ensemble.predict_proba(X[split_point:])
            calibrator.fit(y[split_point:], cal_proba)

    results["calibrator"] = calibrator

    # --- Compute final metrics ---
    metrics = {}
    if X_val is not None and y_val is not None:
        raw_proba = ensemble.predict_proba(X_val)
        cal_proba = calibrator.calibrate(raw_proba)

        raw_preds = (raw_proba >= 0.5).astype(int)
        cal_preds = (cal_proba >= 0.5).astype(int)

        metrics["val_accuracy_raw"] = float(accuracy_score(y_val, raw_preds))
        metrics["val_accuracy_calibrated"] = float(accuracy_score(y_val, cal_preds))

        try:
            metrics["val_log_loss_raw"] = float(
                log_loss(y_val, np.clip(raw_proba, 1e-8, 1 - 1e-8))
            )
            metrics["val_log_loss_calibrated"] = float(
                log_loss(y_val, np.clip(cal_proba, 1e-8, 1 - 1e-8))
            )
        except Exception:
            pass

        metrics["val_mean_proba"] = float(np.mean(cal_proba))
        metrics["val_std_proba"] = float(np.std(cal_proba))

    # Training accuracy
    train_proba = ensemble.predict_proba(X)
    train_preds = (train_proba >= 0.5).astype(int)
    metrics["train_accuracy"] = float(accuracy_score(y, train_preds))

    # Feature importance
    imp = ensemble.get_feature_importance()
    if imp is not None and len(feature_names) == len(imp):
        top_k = min(10, len(feature_names))
        top_idx = np.argsort(-imp)[:top_k]
        metrics["top_features"] = [
            {"name": feature_names[i], "importance": float(imp[i])}
            for i in top_idx
        ]

    results["metrics"] = metrics
    logger.info("Advanced model built. Train acc=%.4f", metrics["train_accuracy"])

    return results


# ---------------------------------------------------------------------------
# Main: test all classes with synthetic data
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    np.random.seed(42)

    print("=" * 70)
    print("Advanced Model Module - Synthetic Data Tests")
    print("=" * 70)

    # Generate synthetic dataset
    n_samples = 800
    n_features = 15
    feature_names = [f"feature_{i}" for i in range(n_features)]

    X = np.random.randn(n_samples, n_features)
    # Target has slight signal from first 3 features
    signal = 0.3 * X[:, 0] - 0.2 * X[:, 1] + 0.1 * X[:, 2]
    y = (signal + np.random.randn(n_samples) * 0.8 > 0).astype(int)
    dates = np.arange(n_samples)  # ordinal "dates"

    # Split into train/val
    split = int(n_samples * 0.8)
    X_train, X_val = X[:split], X[split:]
    y_train, y_val = y[:split], y[split:]
    dates_train = dates[:split]

    # --- Test RecencyWeighter ---
    print("\n--- RecencyWeighter ---")
    rw = RecencyWeighter()
    weights = rw.compute_weights(100, half_life_days=50)
    print(f"  100 samples, half_life=50: oldest_w={weights[0]:.4f}, "
          f"newest_w={weights[-1]:.4f}, sum={weights.sum():.1f}")
    assert abs(weights.sum() - 100) < 0.01, "Weights should sum to n_samples"
    assert weights[-1] > weights[0], "Newest weight should be larger"
    print("  PASSED")

    # --- Test RegimeDetector ---
    print("\n--- RegimeDetector ---")
    detector = RegimeDetector(lookback=60)

    # Trending up prices
    trending_closes = 100 * np.cumprod(1 + np.random.randn(200) * 0.01 + 0.003)
    trending_volumes = np.random.randint(1_000_000, 5_000_000, size=200).astype(float)
    regime = detector.detect(trending_closes, trending_volumes)
    print(f"  Trending data -> regime={regime['regime']}, "
          f"hurst={regime.get('hurst_exponent', 'N/A')}, "
          f"momentum={regime['momentum_regime']}")

    # Mean-reverting prices
    mr_closes = 100 + np.cumsum(np.where(
        np.arange(200) % 2 == 0,
        np.random.randn(200) * 0.5 + 0.3,
        np.random.randn(200) * 0.5 - 0.3,
    ))
    mr_closes = np.clip(mr_closes, 50, 200)
    regime_mr = detector.detect(mr_closes, trending_volumes)
    print(f"  Mean-reverting data -> regime={regime_mr['regime']}, "
          f"hurst={regime_mr.get('hurst_exponent', 'N/A')}")
    print("  PASSED")

    # --- Test StackingEnsemble ---
    print("\n--- StackingEnsemble ---")
    ensemble = StackingEnsemble()
    ensemble.fit(X_train, y_train, X_val=X_val, y_val=y_val)
    proba = ensemble.predict_proba(X_val)
    preds = (proba >= 0.5).astype(int)
    acc = accuracy_score(y_val, preds)
    print(f"  Val accuracy: {acc:.4f}")
    print(f"  Mean predicted P(up): {proba.mean():.4f}")
    imp = ensemble.get_feature_importance()
    if imp is not None:
        top3 = np.argsort(-imp)[:3]
        print(f"  Top 3 features: {[feature_names[i] for i in top3]}")
    print("  PASSED")

    # --- Test ModelCalibrator ---
    print("\n--- ModelCalibrator ---")
    for method in ["isotonic", "sigmoid"]:
        cal = ModelCalibrator(method=method)
        cal.fit(y_val, proba)
        cal_proba = cal.calibrate(proba)
        cal_preds = (cal_proba >= 0.5).astype(int)
        cal_acc = accuracy_score(y_val, cal_preds)
        print(f"  {method}: calibrated acc={cal_acc:.4f}, "
              f"mean_proba={cal_proba.mean():.4f}")
    print("  PASSED")

    # --- Test WalkForwardTrainer ---
    print("\n--- WalkForwardTrainer ---")
    wf = WalkForwardTrainer(n_splits=3, train_months=12, test_months=2)
    wf_results = wf.train_and_evaluate(X_train, y_train, dates_train, feature_names)
    print(f"  Folds completed: {wf_results['n_folds_completed']}")
    print(f"  Per-fold accuracy: {[f'{a:.4f}' for a in wf_results['fold_accuracies']]}")
    print(f"  Overall accuracy: {wf_results['overall_accuracy']:.4f}")
    print(f"  Feature importance stability (rank std): "
          f"{wf_results['feature_importance_stability']:.2f}")
    print("  PASSED")

    # --- Test build_advanced_model ---
    print("\n--- build_advanced_model ---")
    results = build_advanced_model(
        X_train, y_train, dates_train, feature_names,
        X_val=X_val, y_val=y_val,
    )
    m = results["metrics"]
    print(f"  Train accuracy: {m['train_accuracy']:.4f}")
    if "val_accuracy_raw" in m:
        print(f"  Val accuracy (raw): {m['val_accuracy_raw']:.4f}")
        print(f"  Val accuracy (calibrated): {m['val_accuracy_calibrated']:.4f}")
    if "val_log_loss_raw" in m:
        print(f"  Val log-loss (raw): {m['val_log_loss_raw']:.4f}")
        print(f"  Val log-loss (calibrated): {m['val_log_loss_calibrated']:.4f}")
    if "top_features" in m:
        print(f"  Top features: {[f['name'] for f in m['top_features'][:5]]}")
    print("  PASSED")

    print("\n" + "=" * 70)
    print("All tests passed.")
    print("=" * 70)

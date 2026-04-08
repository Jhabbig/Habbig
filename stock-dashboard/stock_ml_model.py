#!/usr/bin/env python3
"""
Stock Up/Down ML Predictor — 5-Year Training Window

Trains XGBoost + Random Forest ensemble on 5 years of daily OHLCV data
for AAPL, TSLA, MSFT, GOOGL. Features include technical indicators,
calendar effects, cross-asset signals, and lagged returns.

Can be run standalone to train + evaluate, or imported by the bot.

Usage:
    python3 stock_ml_model.py              # Train all models + show backtest
    python3 stock_ml_model.py --predict     # Train + predict today
"""

import json
import pickle
import argparse
import warnings
from pathlib import Path
from datetime import datetime, timezone, timedelta

import numpy as np

try:
    import yfinance as yf
except ImportError:
    raise ImportError("yfinance is required: pip install yfinance")

try:
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.metrics import accuracy_score, classification_report
except ImportError:
    raise ImportError("scikit-learn is required: pip install scikit-learn")

try:
    from xgboost import XGBClassifier
except ImportError:
    raise ImportError("xgboost is required: pip install xgboost")

warnings.filterwarnings("ignore")

# ─── Import enhanced modules ─────────────────────────────────────────
try:
    from advanced_model import StackingEnsemble, ModelCalibrator, RegimeDetector, RecencyWeighter
    ADVANCED_ML = True
except ImportError:
    ADVANCED_ML = False

try:
    from sentiment_signals import build_sentiment_features
    SENTIMENT_AVAILABLE = True
except ImportError:
    SENTIMENT_AVAILABLE = False

try:
    from enhanced_data import build_enhanced_features
    ENHANCED_DATA = True
except ImportError:
    ENHANCED_DATA = False

# ─── Config ───────────────────────────────────────────────────────────
MODEL_DIR = Path(__file__).parent / "ml_models"
MODEL_DIR.mkdir(exist_ok=True)

TICKERS = {
    "aapl":  "AAPL",
    "tsla":  "TSLA",
    "msft":  "MSFT",
    "googl": "GOOGL",
}

MARKET_TICKER = "SPY"
VIX_TICKER = "^VIX"

TRAIN_YEARS = 5
# Hold out last 6 months for validation
VALIDATION_DAYS = 126


# ─── Feature Engineering ─────────────────────────────────────────────
def compute_rsi(closes, period=14):
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    rsi = np.full(len(closes), 50.0)
    for i in range(period, len(deltas)):
        avg_gain = np.mean(gains[i - period:i])
        avg_loss = np.mean(losses[i - period:i])
        if avg_loss == 0:
            rsi[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i + 1] = 100 - (100 / (1 + rs))
    return rsi


def compute_ema(data, span):
    alpha = 2 / (span + 1)
    ema = np.zeros_like(data, dtype=float)
    ema[0] = data[0]
    for i in range(1, len(data)):
        ema[i] = alpha * data[i] + (1 - alpha) * ema[i - 1]
    return ema


def compute_macd(closes, fast=12, slow=26, signal=9):
    ema_fast = compute_ema(closes, fast)
    ema_slow = compute_ema(closes, slow)
    macd_line = ema_fast - ema_slow
    signal_line = compute_ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def compute_bollinger(closes, period=20, num_std=2):
    bb_pos = np.zeros(len(closes))
    for i in range(period, len(closes)):
        window = closes[i - period:i]
        sma = np.mean(window)
        std = np.std(window)
        if std == 0:
            bb_pos[i] = 0
        else:
            upper = sma + num_std * std
            lower = sma - num_std * std
            bb_pos[i] = (closes[i] - lower) / (upper - lower) * 2 - 1
    return np.clip(bb_pos, -1, 1)


def compute_atr(highs, lows, closes, period=14):
    atr = np.zeros(len(closes))
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        if i < period:
            atr[i] = tr
        else:
            atr[i] = (atr[i - 1] * (period - 1) + tr) / period
    return atr


def compute_obv(closes, volumes):
    obv = np.zeros(len(closes))
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv[i] = obv[i - 1] + volumes[i]
        elif closes[i] < closes[i - 1]:
            obv[i] = obv[i - 1] - volumes[i]
        else:
            obv[i] = obv[i - 1]
    return obv


def rolling_std(data, period):
    result = np.zeros(len(data))
    for i in range(period, len(data)):
        result[i] = np.std(data[i - period:i])
    return result


def rolling_mean(data, period):
    result = np.zeros(len(data))
    for i in range(period, len(data)):
        result[i] = np.mean(data[i - period:i])
    return result


def rolling_corr(x, y, period):
    result = np.zeros(len(x))
    for i in range(period, len(x)):
        if np.std(x[i - period:i]) > 0 and np.std(y[i - period:i]) > 0:
            result[i] = np.corrcoef(x[i - period:i], y[i - period:i])[0, 1]
    return result


def build_features(df, spy_df=None, vix_df=None):
    """
    Build feature matrix from OHLCV dataframe.
    Returns (feature_matrix, feature_names, target).
    """
    closes = df["Close"].values.astype(float)
    opens = df["Open"].values.astype(float)
    highs = df["High"].values.astype(float)
    lows = df["Low"].values.astype(float)
    volumes = df["Volume"].values.astype(float)
    n = len(closes)

    # ─── Returns ─────────────────────────────────────────────────
    returns_1d = np.zeros(n)
    returns_1d[1:] = (closes[1:] - closes[:-1]) / closes[:-1]

    returns_2d = np.zeros(n)
    returns_2d[2:] = (closes[2:] - closes[:-2]) / closes[:-2]

    returns_3d = np.zeros(n)
    returns_3d[3:] = (closes[3:] - closes[:-3]) / closes[:-3]

    returns_5d = np.zeros(n)
    returns_5d[5:] = (closes[5:] - closes[:-5]) / closes[:-5]

    returns_10d = np.zeros(n)
    returns_10d[10:] = (closes[10:] - closes[:-10]) / closes[:-10]

    returns_20d = np.zeros(n)
    returns_20d[20:] = (closes[20:] - closes[:-20]) / closes[:-20]

    # ─── Technical Indicators ────────────────────────────────────
    rsi_14 = compute_rsi(closes, 14)
    rsi_7 = compute_rsi(closes, 7)
    rsi_21 = compute_rsi(closes, 21)

    macd_line, macd_signal, macd_hist = compute_macd(closes)

    bb_20 = compute_bollinger(closes, 20)
    bb_10 = compute_bollinger(closes, 10)

    atr_14 = compute_atr(highs, lows, closes, 14)
    atr_pct = np.where(closes > 0, atr_14 / closes, 0)

    # Moving averages
    sma_5 = rolling_mean(closes, 5)
    sma_10 = rolling_mean(closes, 10)
    sma_20 = rolling_mean(closes, 20)
    sma_50 = rolling_mean(closes, 50)
    sma_200 = rolling_mean(closes, 200)

    ema_9 = compute_ema(closes, 9)
    ema_21 = compute_ema(closes, 21)

    # Price vs MAs
    price_vs_sma20 = np.where(sma_20 > 0, (closes - sma_20) / sma_20, 0)
    price_vs_sma50 = np.where(sma_50 > 0, (closes - sma_50) / sma_50, 0)
    price_vs_sma200 = np.where(sma_200 > 0, (closes - sma_200) / sma_200, 0)
    sma_20_vs_50 = np.where(sma_50 > 0, (sma_20 - sma_50) / sma_50, 0)

    # ─── Volume features ────────────────────────────────────────
    vol_sma_20 = rolling_mean(volumes, 20)
    vol_ratio = np.where(vol_sma_20 > 0, volumes / vol_sma_20, 1)

    obv = compute_obv(closes, volumes)
    obv_sma = rolling_mean(obv, 20)
    obv_signal = np.where(obv_sma != 0, (obv - obv_sma) / (np.abs(obv_sma) + 1), 0)

    # ─── Volatility ──────────────────────────────────────────────
    vol_5d = rolling_std(returns_1d, 5)
    vol_10d = rolling_std(returns_1d, 10)
    vol_20d = rolling_std(returns_1d, 20)
    vol_ratio_5_20 = np.where(vol_20d > 0, vol_5d / vol_20d, 1)

    # ─── Candle patterns ────────────────────────────────────────
    body = closes - opens
    body_pct = np.where(opens > 0, body / opens, 0)
    upper_shadow = highs - np.maximum(closes, opens)
    lower_shadow = np.minimum(closes, opens) - lows
    candle_range = highs - lows
    body_ratio = np.where(candle_range > 0, np.abs(body) / candle_range, 0)

    # ─── Gap ─────────────────────────────────────────────────────
    gap = np.zeros(n)
    gap[1:] = (opens[1:] - closes[:-1]) / closes[:-1]

    # ─── Streak ──────────────────────────────────────────────────
    streak = np.zeros(n)
    for i in range(1, n):
        if returns_1d[i] > 0:
            streak[i] = max(streak[i - 1], 0) + 1
        elif returns_1d[i] < 0:
            streak[i] = min(streak[i - 1], 0) - 1

    # ─── Z-score ─────────────────────────────────────────────────
    zscore_20 = np.zeros(n)
    for i in range(20, n):
        window = closes[i - 20:i]
        std = np.std(window)
        if std > 0:
            zscore_20[i] = (closes[i] - np.mean(window)) / std

    # ─── Calendar features ───────────────────────────────────────
    dates = df.index
    day_of_week = np.array([d.weekday() for d in dates], dtype=float)
    day_of_month = np.array([d.day for d in dates], dtype=float)
    month = np.array([d.month for d in dates], dtype=float)
    is_month_end = np.array([1.0 if d.day >= 25 else 0.0 for d in dates])
    is_month_start = np.array([1.0 if d.day <= 5 else 0.0 for d in dates])

    # Quarter turn (Jan, Apr, Jul, Oct)
    is_quarter_start = np.array([1.0 if d.month in [1, 4, 7, 10] and d.day <= 10 else 0.0
                                 for d in dates])

    # ─── Cross-asset features ────────────────────────────────────
    if spy_df is not None:
        spy_closes = spy_df["Close"].reindex(df.index, method="ffill").values.astype(float)
        spy_returns = np.zeros(n)
        spy_returns[1:] = np.diff(spy_closes) / spy_closes[:-1]
        spy_ret_5d = np.zeros(n)
        spy_ret_5d[5:] = (spy_closes[5:] - spy_closes[:-5]) / spy_closes[:-5]
        spy_corr_20 = rolling_corr(returns_1d, spy_returns, 20)
    else:
        spy_returns = np.zeros(n)
        spy_ret_5d = np.zeros(n)
        spy_corr_20 = np.zeros(n)

    if vix_df is not None:
        vix_closes = vix_df["Close"].reindex(df.index, method="ffill").values.astype(float)
        vix_level = vix_closes
        vix_change = np.zeros(n)
        vix_change[1:] = np.diff(vix_closes) / vix_closes[:-1]
        vix_sma_10 = rolling_mean(vix_closes, 10)
        vix_vs_sma = np.where(vix_sma_10 > 0, (vix_closes - vix_sma_10) / vix_sma_10, 0)
    else:
        vix_level = np.full(n, 20.0)
        vix_change = np.zeros(n)
        vix_vs_sma = np.zeros(n)

    # ─── Lagged returns (previous N days' returns as features) ───
    lag_1 = np.roll(returns_1d, 1); lag_1[0] = 0
    lag_2 = np.roll(returns_1d, 2); lag_2[:2] = 0
    lag_3 = np.roll(returns_1d, 3); lag_3[:3] = 0
    lag_5 = np.roll(returns_1d, 5); lag_5[:5] = 0

    # ─── Assemble feature matrix ─────────────────────────────────
    feature_names = [
        # Returns
        "ret_1d", "ret_2d", "ret_3d", "ret_5d", "ret_10d", "ret_20d",
        # Lagged returns
        "lag_1", "lag_2", "lag_3", "lag_5",
        # RSI
        "rsi_7", "rsi_14", "rsi_21",
        # MACD
        "macd_line", "macd_signal", "macd_hist",
        # Bollinger
        "bb_20", "bb_10",
        # ATR
        "atr_pct",
        # MA relationships
        "price_vs_sma20", "price_vs_sma50", "price_vs_sma200", "sma_20_vs_50",
        "ema_9_vs_21",
        # Volume
        "vol_ratio", "obv_signal",
        # Volatility
        "vol_5d", "vol_10d", "vol_20d", "vol_ratio_5_20",
        # Candle
        "body_pct", "body_ratio", "upper_shadow_pct", "lower_shadow_pct",
        # Gap
        "gap",
        # Streak / Z-score
        "streak", "zscore_20",
        # Calendar
        "day_of_week", "day_of_month", "month", "is_month_end",
        "is_month_start", "is_quarter_start",
        # Cross-asset
        "spy_ret_1d", "spy_ret_5d", "spy_corr_20",
        "vix_level", "vix_change", "vix_vs_sma",
    ]

    upper_shadow_pct = np.where(candle_range > 0, upper_shadow / candle_range, 0)
    lower_shadow_pct = np.where(candle_range > 0, lower_shadow / candle_range, 0)
    ema_9_vs_21 = np.where(ema_21 > 0, (ema_9 - ema_21) / ema_21, 0)

    features = np.column_stack([
        returns_1d, returns_2d, returns_3d, returns_5d, returns_10d, returns_20d,
        lag_1, lag_2, lag_3, lag_5,
        rsi_7, rsi_14, rsi_21,
        macd_line, macd_signal, macd_hist,
        bb_20, bb_10,
        atr_pct,
        price_vs_sma20, price_vs_sma50, price_vs_sma200, sma_20_vs_50,
        ema_9_vs_21,
        vol_ratio, obv_signal,
        vol_5d, vol_10d, vol_20d, vol_ratio_5_20,
        body_pct, body_ratio, upper_shadow_pct, lower_shadow_pct,
        gap,
        streak, zscore_20,
        day_of_week, day_of_month, month, is_month_end,
        is_month_start, is_quarter_start,
        spy_returns, spy_ret_5d, spy_corr_20,
        vix_level, vix_change, vix_vs_sma,
    ])

    # Target: next day up (1) or down (0)
    target = np.zeros(n, dtype=int)
    target[:-1] = (closes[1:] > closes[:-1]).astype(int)

    # Replace NaN/Inf
    features = np.nan_to_num(features, nan=0.0, posinf=0.0, neginf=0.0)

    return features, feature_names, target


# ─── Model Training ──────────────────────────────────────────────────
class StockMLModel:
    def __init__(self, ticker_key, ticker_yf):
        self.ticker_key = ticker_key
        self.ticker_yf = ticker_yf
        self.stacking_model = None     # StackingEnsemble or fallback models
        self.calibrator = None         # ModelCalibrator
        self.regime_detector = None    # RegimeDetector
        self.scaler = None
        self.feature_names = None
        self.train_accuracy = 0
        self.val_accuracy = 0
        self.high_conf_accuracy = 0
        self.model_path = MODEL_DIR / f"{ticker_key}_v2_model.pkl"
        # Fallback simple models when advanced_model not available
        self.xgb_model = None
        self.rf_model = None
        self.gb_model = None

    def fetch_data(self):
        """Fetch 5 years of daily data."""
        print(f"  [{self.ticker_yf}] Fetching 5 years of daily data...")
        stock = yf.Ticker(self.ticker_yf)
        df = stock.history(period="5y", interval="1d")
        print(f"  [{self.ticker_yf}] Got {len(df)} trading days "
              f"({df.index[0].strftime('%Y-%m-%d')} to {df.index[-1].strftime('%Y-%m-%d')})")
        return df

    def train(self, spy_df=None, vix_df=None):
        """Train the ensemble model with all improvements."""
        df = self.fetch_data()
        if len(df) < 500:
            print(f"  [{self.ticker_yf}] Not enough data ({len(df)} days), skipping")
            return False

        print(f"  [{self.ticker_yf}] Engineering {len(df)} samples...")
        features, feature_names, target = build_features(df, spy_df, vix_df)
        self.feature_names = feature_names

        # Skip first 200 days (need lookback for SMA200 etc)
        start_idx = 200
        X = features[start_idx:-1]
        y = target[start_idx:-1]

        n_samples = len(X)
        n_val = min(VALIDATION_DAYS, n_samples // 5)
        n_train = n_samples - n_val

        X_train, X_val = X[:n_train], X[n_train:]
        y_train, y_val = y[:n_train], y[n_train:]

        print(f"  [{self.ticker_yf}] Training: {n_train} samples, Validation: {n_val} samples")
        print(f"  [{self.ticker_yf}] Target balance: {np.mean(y_train):.1%} up days (train), "
              f"{np.mean(y_val):.1%} up days (val)")

        # Scale features
        self.scaler = StandardScaler()
        X_train_scaled = self.scaler.fit_transform(X_train)
        X_val_scaled = self.scaler.transform(X_val)

        # ─── Recency weighting ───────────────────────────────────
        sample_weights = None
        if ADVANCED_ML:
            try:
                weighter = RecencyWeighter()
                sample_weights = weighter.compute_weights(n_train, half_life_days=504)
                print(f"  [{self.ticker_yf}] Applied recency weighting (half-life=2yr)")
            except Exception:
                pass

        # ─── Train with Stacking Ensemble or fallback ────────────
        if ADVANCED_ML:
            print(f"  [{self.ticker_yf}] Training Stacking Ensemble (XGB+RF+GB → LogReg)...")
            try:
                self.stacking_model = StackingEnsemble()
                # Pass raw (unscaled) features — StackingEnsemble has its own scaler
                self.stacking_model.fit(
                    X_train, y_train,
                    X_val=X_val, y_val=y_val,
                    sample_weight=sample_weights,
                )

                # Get ensemble predictions
                # Pass raw (unscaled) features — StackingEnsemble has its own scaler
                ensemble_proba = self.stacking_model.predict_proba(X_val)
                ensemble_pred = (ensemble_proba >= 0.5).astype(int)
                self.val_accuracy = accuracy_score(y_val, ensemble_pred)

                train_proba = self.stacking_model.predict_proba(X_train)
                self.train_accuracy = accuracy_score(y_train, (train_proba >= 0.5).astype(int))

                print(f"  [{self.ticker_yf}] Stacking Ensemble accuracy:")
                print(f"    Train: {self.train_accuracy:.1%}  Val: {self.val_accuracy:.1%}")

                # Calibrate probabilities
                print(f"  [{self.ticker_yf}] Calibrating probabilities...")
                self.calibrator = ModelCalibrator(method='isotonic')
                self.calibrator.fit(y_val, ensemble_proba)
                calibrated_proba = self.calibrator.calibrate(ensemble_proba)
                cal_pred = (calibrated_proba >= 0.5).astype(int)
                cal_acc = accuracy_score(y_val, cal_pred)
                print(f"    Calibrated val accuracy: {cal_acc:.1%}")

                # Use calibrated accuracy if better
                if cal_acc >= self.val_accuracy:
                    self.val_accuracy = cal_acc
                    ensemble_proba = calibrated_proba

            except Exception as e:
                print(f"  [{self.ticker_yf}] Stacking failed ({e}), falling back to simple ensemble")
                ADVANCED_ML_FAILED = True
                self.stacking_model = None
        else:
            ADVANCED_ML_FAILED = True

        # Fallback: simple ensemble
        if self.stacking_model is None:
            self._train_simple(X_train_scaled, y_train, X_val_scaled, y_val, sample_weights)
            xgb_proba = self.xgb_model.predict_proba(X_val_scaled)[:, 1]
            rf_proba = self.rf_model.predict_proba(X_val_scaled)[:, 1]
            gb_proba = self.gb_model.predict_proba(X_val_scaled)[:, 1]
            ensemble_proba = 0.45 * xgb_proba + 0.30 * rf_proba + 0.25 * gb_proba
            ensemble_pred = (ensemble_proba >= 0.5).astype(int)
            self.val_accuracy = accuracy_score(y_val, ensemble_pred)

        # ─── Regime detection ────────────────────────────────────
        if ADVANCED_ML:
            try:
                closes = df["Close"].values.astype(float)
                volumes = df["Volume"].values.astype(float)
                highs = df["High"].values.astype(float) if "High" in df.columns else None
                lows = df["Low"].values.astype(float) if "Low" in df.columns else None
                self.regime_detector = RegimeDetector(lookback=60)
                regime = self.regime_detector.detect(closes, volumes, highs=highs, lows=lows)
                print(f"  [{self.ticker_yf}] Current regime: {regime.get('regime', '?')} "
                      f"(trend={regime.get('trend_strength', 0):.2f}, "
                      f"vol={regime.get('volatility_regime', '?')})")
            except Exception:
                pass

        # ─── High-confidence accuracy ────────────────────────────
        high_conf_mask = np.abs(ensemble_proba - 0.5) > 0.08
        if high_conf_mask.sum() > 0:
            self.high_conf_accuracy = accuracy_score(
                y_val[high_conf_mask],
                (ensemble_proba[high_conf_mask] >= 0.5).astype(int))
            print(f"  [{self.ticker_yf}] High-confidence val accuracy (>58%): "
                  f"{self.high_conf_accuracy:.1%} ({high_conf_mask.sum()} of {n_val} days)")

        # ─── Feature importance ──────────────────────────────────
        try:
            if self.stacking_model is not None:
                importances = self.stacking_model.get_feature_importance()
            else:
                importances = self.xgb_model.feature_importances_
            top_indices = np.argsort(importances)[-10:][::-1]
            print(f"  [{self.ticker_yf}] Top 10 features:")
            for idx in top_indices:
                if idx < len(feature_names):
                    print(f"    {feature_names[idx]:20s} {importances[idx]:.4f}")
        except Exception:
            pass

        # ─── Simulated P/L ───────────────────────────────────────
        sim_pnl = 0
        sim_wins = 0
        sim_total = 0
        for i in range(len(ensemble_proba)):
            conf = abs(ensemble_proba[i] - 0.5)
            if conf > 0.08:
                predicted_up = ensemble_proba[i] > 0.5
                actual_up = y_val[i] == 1
                sim_total += 1
                if predicted_up == actual_up:
                    sim_wins += 1
                    sim_pnl += 100
                else:
                    sim_pnl -= 100

        if sim_total > 0:
            print(f"  [{self.ticker_yf}] Simulated P/L (val): ${sim_pnl:+,.0f} "
                  f"({sim_wins}/{sim_total} = {sim_wins/sim_total:.1%} win rate)")

        print()
        return True

    def _train_simple(self, X_train, y_train, X_val, y_val, sample_weights=None):
        """Fallback: train simple XGB+RF+GB ensemble."""
        print(f"  [{self.ticker_yf}] Training simple ensemble (XGB+RF+GB)...")
        self.xgb_model = XGBClassifier(
            n_estimators=200, max_depth=2, learning_rate=0.005,
            subsample=0.6, colsample_bytree=0.5, min_child_weight=50,
            reg_alpha=5.0, reg_lambda=10.0, gamma=2.0,
            use_label_encoder=False, eval_metric="logloss",
            random_state=42, verbosity=0,
        )
        self.xgb_model.fit(X_train, y_train, eval_set=[(X_val, y_val)],
                           verbose=False, sample_weight=sample_weights)

        self.rf_model = RandomForestClassifier(
            n_estimators=500, max_depth=3, min_samples_leaf=50,
            min_samples_split=100, max_features=0.3, random_state=42, n_jobs=-1,
        )
        self.rf_model.fit(X_train, y_train,
                          sample_weight=sample_weights)

        self.gb_model = GradientBoostingClassifier(
            n_estimators=150, max_depth=2, learning_rate=0.005,
            subsample=0.6, min_samples_leaf=50, max_features=0.4, random_state=42,
        )
        self.gb_model.fit(X_train, y_train,
                          sample_weight=sample_weights)

        train_proba = (0.45 * self.xgb_model.predict_proba(X_train)[:, 1] +
                       0.30 * self.rf_model.predict_proba(X_train)[:, 1] +
                       0.25 * self.gb_model.predict_proba(X_train)[:, 1])
        self.train_accuracy = accuracy_score(y_train, (train_proba >= 0.5).astype(int))

    def save(self):
        """Save model to disk."""
        data = {
            "stacking_model": self.stacking_model,
            "calibrator": self.calibrator,
            "regime_detector": self.regime_detector,
            "xgb": self.xgb_model,
            "rf": self.rf_model,
            "gb": self.gb_model,
            "scaler": self.scaler,
            "feature_names": self.feature_names,
            "train_accuracy": self.train_accuracy,
            "val_accuracy": self.val_accuracy,
            "high_conf_accuracy": self.high_conf_accuracy,
            "trained_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(self.model_path, "wb") as f:
            pickle.dump(data, f)
        print(f"  [{self.ticker_yf}] Model saved to {self.model_path}")

    def load(self):
        """Load model from disk."""
        if not self.model_path.exists():
            # Try loading old v1 model as fallback
            v1_path = MODEL_DIR / f"{self.ticker_key}_model.pkl"
            if v1_path.exists():
                return self._load_v1(v1_path)
            return False
        try:
            with open(self.model_path, "rb") as f:
                data = pickle.load(f)
            self.stacking_model = data.get("stacking_model")
            self.calibrator = data.get("calibrator")
            self.regime_detector = data.get("regime_detector")
            self.xgb_model = data.get("xgb")
            self.rf_model = data.get("rf")
            self.gb_model = data.get("gb")
            self.scaler = data["scaler"]
            self.feature_names = data["feature_names"]
            self.train_accuracy = data.get("train_accuracy", 0)
            self.val_accuracy = data.get("val_accuracy", 0)
            self.high_conf_accuracy = data.get("high_conf_accuracy", 0)
            return True
        except Exception as e:
            print(f"  [{self.ticker_yf}] Error loading model: {e}")
            return False

    def _load_v1(self, path):
        """Load old v1 model format."""
        try:
            with open(path, "rb") as f:
                data = pickle.load(f)
            self.xgb_model = data["xgb"]
            self.rf_model = data["rf"]
            self.gb_model = data["gb"]
            self.scaler = data["scaler"]
            self.feature_names = data["feature_names"]
            self.train_accuracy = data.get("train_accuracy", 0)
            self.val_accuracy = data.get("val_accuracy", 0)
            return True
        except Exception:
            return False

    def predict(self, spy_df=None, vix_df=None):
        """
        Predict today's direction using all available signals.
        Returns (direction, confidence, details_dict).
        """
        if self.stacking_model is None and self.xgb_model is None:
            if not self.load():
                return None, 0.5, {}

        # Fetch recent data
        stock = yf.Ticker(self.ticker_yf)
        df = stock.history(period="1y", interval="1d")
        if len(df) < 200:
            return None, 0.5, {}

        features, _, _ = build_features(df, spy_df, vix_df)
        X_today = features[-1:].copy()
        X_today = np.nan_to_num(X_today, nan=0.0, posinf=0.0, neginf=0.0)
        X_today_scaled = self.scaler.transform(X_today)

        # Get prediction from stacking or simple ensemble
        if self.stacking_model is not None:
            # Pass raw (unscaled) features — StackingEnsemble has its own scaler
            raw_proba = self.stacking_model.predict_proba(X_today)
            # Handle both scalar and array output
            if hasattr(raw_proba, '__len__'):
                ensemble_proba = float(raw_proba.ravel()[0])
            else:
                ensemble_proba = float(raw_proba)
            # Apply calibration if available
            if self.calibrator is not None:
                try:
                    cal_result = self.calibrator.calibrate(np.array([ensemble_proba]))
                    if hasattr(cal_result, '__len__'):
                        ensemble_proba = float(cal_result.ravel()[0])
                    else:
                        ensemble_proba = float(cal_result)
                except Exception:
                    pass
            details = {"model_type": "stacking_ensemble"}
        else:
            xgb_proba = self.xgb_model.predict_proba(X_today_scaled)[0, 1]
            rf_proba = self.rf_model.predict_proba(X_today_scaled)[0, 1]
            gb_proba = self.gb_model.predict_proba(X_today_scaled)[0, 1]
            ensemble_proba = 0.45 * xgb_proba + 0.30 * rf_proba + 0.25 * gb_proba
            details = {
                "model_type": "simple_ensemble",
                "xgb_up_prob": round(float(xgb_proba), 4),
                "rf_up_prob": round(float(rf_proba), 4),
                "gb_up_prob": round(float(gb_proba), 4),
            }

        # Add enhanced real-time features at prediction time
        enhanced_boost = 0.0
        if ENHANCED_DATA:
            try:
                enh = build_enhanced_features(self.ticker_key, self.ticker_yf)
                # Pre-market signal is strongest real-time signal
                pm_change = enh.get("pm_premarket_change_pct", 0)
                if abs(pm_change) > 0.5:
                    enhanced_boost = np.sign(pm_change) * min(abs(pm_change) * 0.01, 0.05)
                # Futures signal
                futures_change = enh.get("pm_futures_es_change", 0)
                if abs(futures_change) > 0.3:
                    enhanced_boost += np.sign(futures_change) * min(abs(futures_change) * 0.005, 0.02)
                # International markets signal
                intl_avg = np.mean([enh.get(f"intl_{k}_ret", 0) for k in
                                    ["nikkei", "dax", "ftse"]])
                if abs(intl_avg) > 0.5:
                    enhanced_boost += np.sign(intl_avg) * 0.01
                details["enhanced_boost"] = round(enhanced_boost, 4)
                details["premarket_pct"] = round(pm_change, 3)
                details["futures_es"] = round(futures_change, 3)
            except Exception:
                pass

        if SENTIMENT_AVAILABLE:
            try:
                sent = build_sentiment_features(self.ticker_key, self.ticker_yf)
                # Earnings proximity affects confidence
                days_to_earn = sent.get("earn_days_to_earnings", 999)
                if days_to_earn <= 3:
                    # Near earnings = high uncertainty, reduce confidence
                    enhanced_boost *= 0.5
                    details["near_earnings"] = True
                # News sentiment nudge
                news_sent = sent.get("news_sentiment_score", 0)
                if abs(news_sent) > 0.2:
                    enhanced_boost += np.sign(news_sent) * 0.01
                details["news_sentiment"] = round(news_sent, 3)
                details["days_to_earnings"] = int(days_to_earn)
            except Exception:
                pass

        # Apply enhanced boost
        ensemble_proba = np.clip(ensemble_proba + enhanced_boost, 0.01, 0.99)

        # Regime adjustment
        if self.regime_detector is not None:
            try:
                closes = df["Close"].values.astype(float)
                volumes = df["Volume"].values.astype(float)
                highs = df["High"].values.astype(float) if "High" in df.columns else None
                lows = df["Low"].values.astype(float) if "Low" in df.columns else None
                regime = self.regime_detector.detect(closes, volumes, highs=highs, lows=lows)
                details["regime"] = regime.get("regime", "unknown")
                details["volatility_regime"] = regime.get("volatility_regime", "normal")
                # High volatility = lower confidence
                if regime.get("volatility_regime") == "high":
                    ensemble_proba = 0.5 + (ensemble_proba - 0.5) * 0.7
            except Exception:
                pass

        direction = "up" if ensemble_proba > 0.5 else "down"
        confidence = ensemble_proba if direction == "up" else (1 - ensemble_proba)

        details.update({
            "ensemble_up_prob": round(float(ensemble_proba), 4),
            "direction": direction,
            "confidence": round(float(confidence), 4),
            "val_accuracy": round(self.val_accuracy, 4),
            "high_conf_accuracy": round(self.high_conf_accuracy, 4),
        })

        return direction, confidence, details


# ─── Public API for bot integration ──────────────────────────────────
_model_cache = {}


def get_ml_stock_prediction(ticker_key, spy_df=None, vix_df=None):
    """
    Get ML prediction for a stock ticker.
    Returns (direction, confidence, details) or (None, 0.5, {}).
    Loads cached model from disk if available.
    """
    if ticker_key not in TICKERS:
        return None, 0.5, {}

    if ticker_key not in _model_cache:
        model = StockMLModel(ticker_key, TICKERS[ticker_key])
        if model.load():
            _model_cache[ticker_key] = model
        else:
            return None, 0.5, {}

    return _model_cache[ticker_key].predict(spy_df, vix_df)


def train_all_models():
    """Train models for all tickers."""
    print("=" * 60)
    print("Stock ML Model Training — 5 Year Window")
    print("=" * 60)

    # Fetch market data once
    print("\nFetching SPY and VIX data...")
    spy = yf.Ticker(MARKET_TICKER)
    spy_df = spy.history(period="5y", interval="1d")
    print(f"  SPY: {len(spy_df)} days")

    vix = yf.Ticker(VIX_TICKER)
    vix_df = vix.history(period="5y", interval="1d")
    print(f"  VIX: {len(vix_df)} days")
    print()

    results = {}
    for ticker_key, ticker_yf in TICKERS.items():
        print(f"{'─' * 50}")
        print(f"Training {ticker_yf}...")
        print(f"{'─' * 50}")

        model = StockMLModel(ticker_key, ticker_yf)
        success = model.train(spy_df, vix_df)
        if success:
            model.save()
            results[ticker_key] = {
                "train_acc": model.train_accuracy,
                "val_acc": model.val_accuracy,
            }

    # Summary
    print("=" * 60)
    print("Training Summary")
    print("=" * 60)
    for tk, res in results.items():
        print(f"  {TICKERS[tk]:6s}  Train: {res['train_acc']:.1%}  Val: {res['val_acc']:.1%}")

    return results


def predict_all():
    """Load models and predict for all tickers."""
    print("\n" + "=" * 60)
    print("Today's Predictions")
    print("=" * 60)

    spy = yf.Ticker(MARKET_TICKER)
    spy_df = spy.history(period="1y", interval="1d")
    vix = yf.Ticker(VIX_TICKER)
    vix_df = vix.history(period="1y", interval="1d")

    for ticker_key, ticker_yf in TICKERS.items():
        direction, confidence, details = get_ml_stock_prediction(ticker_key, spy_df, vix_df)
        if direction:
            print(f"\n  {ticker_yf}:")
            print(f"    Prediction: {direction.upper()} @ {confidence:.1%}")
            print(f"    Model: {details.get('model_type', 'unknown')} | "
                  f"Ensemble P(up)={details.get('ensemble_up_prob', 0):.1%}")
            print(f"    Val accuracy: {details.get('val_accuracy', 0):.1%} | "
                  f"High-conf: {details.get('high_conf_accuracy', 0):.1%}")
            if details.get('regime'):
                print(f"    Regime: {details['regime']} ({details.get('volatility_regime', '?')})")
            if details.get('premarket_pct'):
                print(f"    Pre-market: {details['premarket_pct']:+.2f}%")
            if details.get('news_sentiment') is not None:
                print(f"    News sentiment: {details['news_sentiment']:+.3f}")
        else:
            print(f"\n  {ticker_yf}: No model available")


def main():
    parser = argparse.ArgumentParser(description="Stock ML Model Trainer")
    parser.add_argument("--predict", action="store_true", help="Train then predict today")
    args = parser.parse_args()

    train_all_models()

    if args.predict:
        predict_all()


if __name__ == "__main__":
    main()

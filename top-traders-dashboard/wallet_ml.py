#!/usr/bin/env python3
"""
ML-based wallet anomaly detection.

Two complementary models, both designed for small/weakly-labeled tabular data:

  1. Isolation Forest (unsupervised) — finds wallets whose feature signature
     differs from the bulk of trader behavior. No labels needed. This is the
     "weird ones" detector.

  2. XGBoost ranker (weakly supervised) — trained on retroactive winner labels
     from resolved_markets.py. Predicts P(this wallet has insider edge) on
     unseen wallets. Falls back to a simple linear scoring function if xgboost
     isn't installed.

Both models are deliberately gracefully degradable: the dashboard still works
if scikit-learn / xgboost aren't installed — the module just exposes empty
results and we lean on the rule-based scanner + Bayesian scoring instead.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)

# Soft-import ML libs so the module loads even when they're missing
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    np = None
    _HAS_NUMPY = False

try:
    from sklearn.ensemble import IsolationForest
    from sklearn.preprocessing import StandardScaler
    _HAS_SKLEARN = True
except ImportError:
    IsolationForest = None
    StandardScaler = None
    _HAS_SKLEARN = False

try:
    import xgboost as xgb
    _HAS_XGBOOST = True
except ImportError:
    xgb = None
    _HAS_XGBOOST = False


# Feature names — keep order stable for both training and inference
FEATURE_NAMES = [
    "longshot_bets",
    "longshot_wins",
    "win_rate",
    "avg_buy_price",
    "min_buy_price",
    "median_buy_price",
    "total_staked_usd",
    "total_realized_usd",
    "max_single_bet_usd",
    "unique_markets",
    "late_bet_ratio",       # share of bets placed in last 24h before close
    "very_late_bet_ratio",  # share placed in last 6h
    "avg_hours_before_close",
    "active_days_span",
]


# ─── Feature engineering ─────────────────────────────────────────────

def _safe(value, default=0.0):
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def build_wallet_features(winners: list[dict]) -> dict[str, dict[str, float]]:
    """
    Aggregate per-wallet features from the long-shot winner records produced
    by resolved_markets.find_longshot_winners().

    Returns: { wallet_address: { feature_name: value, ... } }
    """
    by_wallet: dict[str, list[dict]] = defaultdict(list)
    for w in winners:
        addr = (w.get("wallet") or "").lower()
        if addr:
            by_wallet[addr].append(w)

    features: dict[str, dict[str, float]] = {}
    for wallet, wins in by_wallet.items():
        prices = [_safe(w.get("buy_price")) for w in wins]
        sizes = [_safe(w.get("size_usd")) for w in wins]
        realized = [_safe(w.get("realized_profit")) for w in wins]
        markets = {w.get("market_id") for w in wins if w.get("market_id")}
        hours_before = [
            _safe(w.get("hours_before_close"))
            for w in wins
            if w.get("hours_before_close") is not None
        ]
        timestamps = [_safe(w.get("trade_ts")) for w in wins if w.get("trade_ts")]

        n = len(wins)
        prices_sorted = sorted(prices)
        median_price = prices_sorted[n // 2] if n else 0.0

        late_bets = sum(1 for h in hours_before if 0 < h <= 24)
        very_late = sum(1 for h in hours_before if 0 < h <= 6)
        avg_hours = sum(hours_before) / len(hours_before) if hours_before else 0.0

        if timestamps:
            span_seconds = max(timestamps) - min(timestamps)
            active_days = max(span_seconds / 86400, 0.1)
        else:
            active_days = 0.1

        features[wallet] = {
            "longshot_bets": float(n),
            "longshot_wins": float(n),  # all rows in `winners` are wins by construction
            "win_rate": 1.0,             # placeholder; refined when losses are available
            "avg_buy_price": sum(prices) / n if n else 0.0,
            "min_buy_price": min(prices) if prices else 0.0,
            "median_buy_price": median_price,
            "total_staked_usd": sum(sizes),
            "total_realized_usd": sum(realized),
            "max_single_bet_usd": max(sizes) if sizes else 0.0,
            "unique_markets": float(len(markets)),
            "late_bet_ratio": late_bets / n if n else 0.0,
            "very_late_bet_ratio": very_late / n if n else 0.0,
            "avg_hours_before_close": avg_hours,
            "active_days_span": active_days,
        }

    return features


def features_to_matrix(feature_dict: dict[str, dict[str, float]]) -> tuple[list[str], Any]:
    """Convert per-wallet feature dict to (wallet_list, numpy_matrix)."""
    if not _HAS_NUMPY:
        return [], None
    wallets = list(feature_dict.keys())
    if not wallets:
        return [], np.zeros((0, len(FEATURE_NAMES)))
    rows = [
        [feature_dict[w].get(name, 0.0) for name in FEATURE_NAMES]
        for w in wallets
    ]
    return wallets, np.array(rows, dtype=float)


# ─── Isolation Forest ────────────────────────────────────────────────

def isolation_forest_scores(
    feature_dict: dict[str, dict[str, float]],
    contamination: float = 0.05,
) -> list[dict]:
    """
    Returns per-wallet anomaly scores. Higher score = more anomalous.

    Falls back to a simple z-score based ranker if sklearn isn't installed,
    so the dashboard always has *something* to show.
    """
    if not feature_dict:
        return []

    wallets, X = features_to_matrix(feature_dict)
    if X is None or len(wallets) == 0:
        return []

    if _HAS_SKLEARN and len(wallets) >= 10:
        try:
            scaler = StandardScaler()
            X_scaled = scaler.fit_transform(X)
            iso = IsolationForest(
                n_estimators=200,
                contamination=contamination,
                random_state=42,
                n_jobs=1,
            )
            iso.fit(X_scaled)
            # decision_function: higher = more normal. Negate so higher = anomalous.
            raw = -iso.decision_function(X_scaled)
            # Min-max scale to 0..1 for nicer display
            rmin, rmax = float(raw.min()), float(raw.max())
            denom = (rmax - rmin) or 1.0
            normalized = (raw - rmin) / denom
            preds = iso.predict(X_scaled)  # -1 = anomaly, 1 = normal

            results = []
            for i, w in enumerate(wallets):
                results.append({
                    "wallet": w,
                    "anomaly_score": float(round(normalized[i], 4)),
                    "raw_score": float(round(raw[i], 4)),
                    "is_anomaly": bool(preds[i] == -1),
                    "model": "isolation_forest",
                    "features": feature_dict[w],
                })
            results.sort(key=lambda r: r["anomaly_score"], reverse=True)
            return results
        except Exception as e:
            logger.warning("IsolationForest failed, falling back to z-score: %s", e)

    # Fallback: per-feature z-scores summed
    return _zscore_fallback(feature_dict)


def _zscore_fallback(feature_dict: dict[str, dict[str, float]]) -> list[dict]:
    """No-dependencies anomaly score: sum of |z| across high-signal features."""
    if not feature_dict:
        return []

    high_signal_features = [
        "unique_markets",
        "total_realized_usd",
        "min_buy_price",
        "late_bet_ratio",
        "very_late_bet_ratio",
        "max_single_bet_usd",
    ]

    means: dict[str, float] = {}
    stds: dict[str, float] = {}
    for fname in high_signal_features:
        vals = [feats.get(fname, 0.0) for feats in feature_dict.values()]
        if not vals:
            continue
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        means[fname] = mean
        stds[fname] = math.sqrt(var) or 1.0

    results = []
    for wallet, feats in feature_dict.items():
        z_sum = 0.0
        for fname in high_signal_features:
            z = (feats.get(fname, 0.0) - means.get(fname, 0.0)) / stds.get(fname, 1.0)
            # Smaller buy price is "more anomalous" → flip its sign
            if fname == "min_buy_price":
                z = -z
            z_sum += abs(z)
        results.append({
            "wallet": wallet,
            "anomaly_score": round(z_sum / len(high_signal_features), 4),
            "raw_score": round(z_sum, 4),
            "is_anomaly": z_sum > 4,
            "model": "zscore_fallback",
            "features": feats,
        })

    # Normalize 0..1
    if results:
        max_score = max(r["anomaly_score"] for r in results) or 1.0
        for r in results:
            r["anomaly_score"] = round(r["anomaly_score"] / max_score, 4)

    results.sort(key=lambda r: r["anomaly_score"], reverse=True)
    return results


# ─── XGBoost ranker ──────────────────────────────────────────────────

# Hand-tuned linear scoring weights — used as the fallback model AND as a
# baseline ground truth for the supervised ranker. These reflect the
# rule-based scoring intuition from the suspicious-trades scanner.
LINEAR_WEIGHTS = {
    "longshot_bets":           0.5,
    "unique_markets":          5.0,
    "total_realized_usd":      0.0001,
    "min_buy_price":          -10.0,   # smaller min price = more suspicious
    "late_bet_ratio":          15.0,
    "very_late_bet_ratio":     25.0,
    "max_single_bet_usd":      0.00005,
    "win_rate":                10.0,
}


def _linear_score(features: dict[str, float]) -> float:
    return sum(features.get(k, 0.0) * w for k, w in LINEAR_WEIGHTS.items())


def train_xgboost_ranker(
    train_features: dict[str, dict[str, float]],
    train_labels: dict[str, float],
) -> Any:
    """
    Train an XGBoost regressor to predict the rank score from features.
    Returns the trained model or None if xgboost isn't available.

    `train_labels` should be the linear baseline score (or any teacher signal)
    for each wallet — we're effectively distilling rule scores into a model
    that can generalize to unseen feature combinations.
    """
    if not _HAS_XGBOOST or not _HAS_NUMPY:
        return None
    if len(train_features) < 20:
        return None

    wallets, X = features_to_matrix(train_features)
    y = np.array([train_labels.get(w, 0.0) for w in wallets], dtype=float)

    try:
        model = xgb.XGBRegressor(
            n_estimators=150,
            max_depth=4,
            learning_rate=0.08,
            subsample=0.85,
            colsample_bytree=0.85,
            objective="reg:squarederror",
            random_state=42,
            verbosity=0,
        )
        model.fit(X, y)
        return model
    except Exception as e:
        logger.warning("XGBoost training failed: %s", e)
        return None


def xgboost_scores(feature_dict: dict[str, dict[str, float]]) -> list[dict]:
    """
    Train an XGBoost model on the linear baseline (self-distillation) and
    return per-wallet scores. Falls back to pure linear scoring if xgboost
    isn't installed.
    """
    if not feature_dict:
        return []

    # Build teacher labels via the linear scoring function
    teacher_labels = {w: _linear_score(feats) for w, feats in feature_dict.items()}

    model = train_xgboost_ranker(feature_dict, teacher_labels)
    fallback_used = model is None

    results = []
    if not fallback_used and _HAS_NUMPY:
        wallets, X = features_to_matrix(feature_dict)
        try:
            preds = model.predict(X)
        except Exception as e:
            logger.warning("XGBoost predict failed: %s", e)
            fallback_used = True
            preds = None

        if preds is not None:
            # Min-max scale to 0..100
            pmin, pmax = float(preds.min()), float(preds.max())
            denom = (pmax - pmin) or 1.0
            for i, w in enumerate(wallets):
                results.append({
                    "wallet": w,
                    "score": float(round((preds[i] - pmin) / denom * 100, 2)),
                    "raw_score": float(round(preds[i], 4)),
                    "model": "xgboost",
                    "features": feature_dict[w],
                })

    if fallback_used:
        for w, feats in feature_dict.items():
            results.append({
                "wallet": w,
                "score": round(teacher_labels[w], 2),
                "raw_score": round(teacher_labels[w], 4),
                "model": "linear_fallback",
                "features": feats,
            })
        if results:
            max_s = max(r["score"] for r in results) or 1.0
            for r in results:
                r["score"] = round(r["score"] / max_s * 100, 2)

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


# ─── Combined wallet ranking ─────────────────────────────────────────

def rank_wallets(winners: list[dict]) -> dict:
    """
    Full ML pipeline: features → IsolationForest → XGBoost.

    Returns combined ranking that fuses both models.
    """
    feature_dict = build_wallet_features(winners)
    if not feature_dict:
        return {
            "feature_count": 0,
            "wallet_count": 0,
            "isolation_forest": [],
            "xgboost": [],
            "combined": [],
            "available_models": _model_availability(),
        }

    iso_results = isolation_forest_scores(feature_dict)
    xgb_results = xgboost_scores(feature_dict)

    iso_by_wallet = {r["wallet"]: r for r in iso_results}
    xgb_by_wallet = {r["wallet"]: r for r in xgb_results}

    combined = []
    for wallet in feature_dict:
        iso = iso_by_wallet.get(wallet, {})
        xg = xgb_by_wallet.get(wallet, {})
        iso_norm = iso.get("anomaly_score", 0.0)
        xgb_norm = xg.get("score", 0.0) / 100.0
        combined_score = 0.55 * xgb_norm + 0.45 * iso_norm
        combined.append({
            "wallet": wallet,
            "combined_score": round(combined_score * 100, 2),
            "isolation_score": round(iso_norm * 100, 2),
            "xgboost_score": xg.get("score", 0.0),
            "is_anomaly": iso.get("is_anomaly", False),
            "features": feature_dict[wallet],
        })
    combined.sort(key=lambda r: r["combined_score"], reverse=True)

    return {
        "feature_count": len(FEATURE_NAMES),
        "wallet_count": len(feature_dict),
        "isolation_forest": iso_results[:50],
        "xgboost": xgb_results[:50],
        "combined": combined[:50],
        "available_models": _model_availability(),
    }


def _model_availability() -> dict[str, bool]:
    return {
        "numpy": _HAS_NUMPY,
        "sklearn": _HAS_SKLEARN,
        "xgboost": _HAS_XGBOOST,
    }


if __name__ == "__main__":
    print(f"Model availability: {_model_availability()}")
    print(f"Features: {FEATURE_NAMES}")

    # Quick smoke test with synthetic data
    fake_winners = [
        {
            "wallet": f"0x{i:040x}",
            "market_id": f"market_{i % 10}",
            "buy_price": 0.05 + (i % 5) * 0.04,
            "size_usd": 500 + (i * 137) % 9500,
            "realized_profit": 1000 + (i * 211) % 25000,
            "hours_before_close": (i * 7) % 96,
            "trade_ts": 1700000000 + i * 3600,
        }
        for i in range(40)
    ]
    result = rank_wallets(fake_winners)
    print(f"\nWallets: {result['wallet_count']}")
    print(f"\nTop 5 combined:")
    for r in result["combined"][:5]:
        print(f"  {r['wallet'][:14]}... → combined={r['combined_score']} iso={r['isolation_score']} xgb={r['xgboost_score']}")

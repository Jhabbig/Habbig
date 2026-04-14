"""
ML Predictor for Crypto 5-Minute Up/Down — Multi-Coin

Trains on RAW 1-SECOND tick data — sees the microstructure within each
5-minute window, not just OHLCV summaries.

Three models:
1. LSTM — learns temporal patterns across sequences of windows
2. PyTorch feedforward net — learns non-linear microstructure patterns
3. Gradient Boosted Trees — ensemble on engineered features

Supports multiple coins: BTC, ETH, SOL, DOGE, XRP (with pickle data).
"""

import numpy as np
import pickle
import requests
import time
from datetime import datetime, timezone
from pathlib import Path

# ─── Config ──────────────────────────────────────────────────────────

BINANCE_API = "https://api.binance.com/api/v3"
CACHE_DIR = Path(__file__).parent / "cache"
WINDOW_SEC = 300  # 5 minutes
WINDOW_MS = WINDOW_SEC * 1000
LSTM_SEQ_LEN = 10  # number of consecutive windows for LSTM input

COIN_SYMBOLS = {
    "btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT",
    "doge": "DOGEUSDT", "xrp": "XRPUSDT", "bnb": "BNBUSDT",
}

def features_cache_path(symbol):
    return CACHE_DIR / f"{symbol.lower()}_1s_features.npz"


# ─── Load raw 1-second tick data ──────────────────────────────────────

def load_all_tick_data(symbol="BTCUSDT"):
    """
    Load ALL 1-second pickle files for a symbol at FULL resolution.
    Returns two numpy arrays: (timestamps_ms, prices).
    """
    pickle_files = sorted(CACHE_DIR.glob(f"{symbol}_1s_*.pkl"))
    if not pickle_files:
        return np.array([]), np.array([])

    all_ts = {}  # ts -> price, dedup

    for pf in pickle_files:
        print(f"  [ML] Loading {pf.name} ({pf.stat().st_size / 1024 / 1024:.0f}MB)...")
        try:
            from btc_analyzer import safe_pickle_load
            data = safe_pickle_load(pf)
        except Exception as e:
            print(f"  [ML] Error: {e}")
            continue

        if not data:
            continue

        if len(data[0]) > 2:
            for row in data:
                all_ts[int(row[0])] = float(row[4])  # close price
        else:
            for row in data:
                all_ts[int(row[0])] = float(row[1])

        del data

    if not all_ts:
        return np.array([]), np.array([])

    sorted_items = sorted(all_ts.items())
    timestamps = np.array([t[0] for t in sorted_items], dtype=np.int64)
    prices = np.array([t[1] for t in sorted_items], dtype=np.float64)
    del all_ts, sorted_items

    days = (timestamps[-1] - timestamps[0]) / 1000 / 86400
    print(f"  [ML] Loaded {len(timestamps):,} ticks spanning {days:.0f} days")
    return timestamps, prices


# ─── Extract micro-features from 1-second data ──────────────────────

def extract_window_features(prices_in_window):
    """
    Extract ~30 microstructure features from the raw 1-second prices
    within a single 5-minute window.

    This is where the 1s resolution pays off — we can see things
    that 5m OHLCV candles hide.
    """
    n = len(prices_in_window)
    if n < 10:
        return None

    p = prices_in_window
    p0 = p[0]  # opening price
    if p0 == 0:
        return None  # corrupted data — can't normalize

    # Normalize prices relative to open (basis points)
    delta = (p - p0) / p0 * 10000  # in basis points

    # ── 1-10: TRAJECTORY SHAPE ──
    # Sample price at 10 evenly-spaced points through the window
    # This captures the "shape" of the move (V, inverted-V, staircase, etc.)
    indices = np.linspace(0, n - 1, 10, dtype=int)
    trajectory = delta[indices]
    # Normalize trajectory to [-1, 1] range
    traj_range = max(abs(delta.max()), abs(delta.min()), 1)
    trajectory_norm = trajectory / traj_range

    # ── 11: TOTAL RETURN ──
    total_return = delta[-1]

    # ── 12-13: MAX EXCURSION ──
    max_up = delta.max()
    max_down = delta.min()

    # ── 14-15: TIME TO PEAK/TROUGH ──
    time_to_peak = np.argmax(delta) / n  # 0.0 to 1.0
    time_to_trough = np.argmin(delta) / n

    # ── 16: DIRECTION CHANGES ──
    # How many times does price reverse direction?
    diffs = np.diff(p)
    signs = np.sign(diffs)
    signs_nonzero = signs[signs != 0]
    if len(signs_nonzero) > 1:
        direction_changes = np.sum(np.diff(signs_nonzero) != 0)
    else:
        direction_changes = 0
    direction_changes_norm = direction_changes / max(n, 1) * 100

    # ── 17: PERCENT TIME ABOVE OPEN ──
    pct_above_open = np.mean(p > p0) * 100

    # ── 18-19: FIRST/LAST 60 SECONDS MOMENTUM ──
    first_60 = min(60, n // 3)
    last_60 = min(60, n // 3)
    first_60s_return = (p[first_60] - p[0]) / p[0] * 10000 if first_60 > 0 and p[0] != 0 else 0
    last_60s_return = (p[-1] - p[-last_60]) / p[-last_60] * 10000 if last_60 > 0 and p[-last_60] != 0 else 0

    # ── 20: ACCELERATION ──
    # First half return vs second half return
    mid = n // 2
    first_half = (p[mid] - p[0]) / p[0] * 10000 if p[0] != 0 else 0
    second_half = (p[-1] - p[mid]) / p[mid] * 10000 if p[mid] != 0 else 0
    acceleration = second_half - first_half

    # ── 21: MICRO-VOLATILITY ──
    denom = p[:-1].copy()
    denom[denom == 0] = 1e-10  # guard against zero-price ticks
    returns_1s = np.diff(p) / denom * 10000
    volatility = np.std(returns_1s) if len(returns_1s) > 1 else 0

    # ── 22: UP-TICK RATIO ──
    up_ticks = np.sum(returns_1s > 0)
    total_ticks = len(returns_1s)
    uptick_ratio = up_ticks / max(total_ticks, 1)

    # ── 23-24: LARGEST SINGLE MOVES ──
    if len(returns_1s) > 0:
        max_up_tick = returns_1s.max()
        max_down_tick = returns_1s.min()
    else:
        max_up_tick = 0
        max_down_tick = 0

    # ── 25: SKEWNESS of returns ──
    if len(returns_1s) > 2 and volatility > 0:
        skewness = float(np.mean(((returns_1s - returns_1s.mean()) / volatility) ** 3))
        if np.isnan(skewness) or np.isinf(skewness):
            skewness = 0
    else:
        skewness = 0

    # ── 26: MEAN REVERSION COUNT ──
    mean_price = np.mean(p)
    above_mean = p > mean_price
    mean_crossings = np.sum(np.diff(above_mean.astype(int)) != 0)
    mean_cross_norm = mean_crossings / max(n, 1) * 100

    # ── 27: UP-VOLUME vs DOWN-VOLUME ──
    # Sum of positive moves vs sum of negative moves
    up_moves = returns_1s[returns_1s > 0].sum() if np.any(returns_1s > 0) else 0
    down_moves = abs(returns_1s[returns_1s < 0].sum()) if np.any(returns_1s < 0) else 0
    vol_ratio = up_moves / max(down_moves, 0.001) if down_moves > 0 else 2.0

    # ── 28: VOLATILITY CLUSTERING ──
    # Is volatility increasing or decreasing through the window?
    if len(returns_1s) > 20:
        first_vol = np.std(returns_1s[:len(returns_1s)//2])
        second_vol = np.std(returns_1s[len(returns_1s)//2:])
        vol_trend = (second_vol - first_vol) / max(first_vol, 0.001)
    else:
        vol_trend = 0

    # ── 29: PRICE RANGE EFFICIENCY ──
    # How much of the high-low range was captured by the close?
    price_range = max(p) - min(p)
    if price_range > 0:
        efficiency = (p[-1] - min(p)) / price_range  # 1.0 = closed at high
    else:
        efficiency = 0.5

    # ── 30: TREND LINEARITY ──
    # R-squared of linear fit — high = clean trend, low = choppy
    if n > 5:
        x = np.arange(n, dtype=np.float64)
        slope = np.polyfit(x, delta, 1)[0]
        predicted = slope * x
        ss_res = np.sum((delta - predicted) ** 2)
        ss_tot = np.sum((delta - delta.mean()) ** 2)
        r_squared = 1 - ss_res / max(ss_tot, 0.001) if ss_tot > 0 else 0
    else:
        r_squared = 0
        slope = 0

    # Combine all features
    features = np.concatenate([
        trajectory_norm,                     # 1-10: shape
        [total_return / 100],                # 11: total return (scaled)
        [max_up / 100, max_down / 100],      # 12-13: excursion (scaled)
        [time_to_peak, time_to_trough],      # 14-15: timing
        [direction_changes_norm],            # 16: choppiness
        [pct_above_open / 100],              # 17: time above open
        [first_60s_return / 100],            # 18: early momentum
        [last_60s_return / 100],             # 19: late momentum
        [acceleration / 100],                # 20: acceleration
        [volatility],                        # 21: micro-vol
        [uptick_ratio],                      # 22: buying pressure
        [max_up_tick, max_down_tick],         # 23-24: extreme ticks
        [skewness],                          # 25: skew
        [mean_cross_norm],                   # 26: mean reversion
        [min(vol_ratio, 5.0) / 5.0],         # 27: up/down volume ratio
        [vol_trend],                         # 28: vol clustering
        [efficiency],                        # 29: range efficiency
        [r_squared],                         # 30: trend linearity
    ])

    return features.astype(np.float32)


# ─── Build training data from 1-second ticks ─────────────────────────

def build_windows_and_features(timestamps, prices):
    """
    Split tick data into 5-minute windows and extract features.
    Returns: window_features (N x 30), window_outcomes (N,), window_times (N,)
    """
    if len(timestamps) < 1000:
        return np.array([]), np.array([]), np.array([])

    first_ts = int(timestamps[0])
    aligned_start = first_ts - (first_ts % WINDOW_MS)

    window_features = []
    window_outcomes = []  # 1 = up, 0 = down
    window_times = []

    current_start = aligned_start
    idx = 0
    n = len(timestamps)

    while idx < n:
        window_end = current_start + WINDOW_MS

        # Collect ticks in this window
        start_idx = idx
        while idx < n and timestamps[idx] < window_end:
            idx += 1
        end_idx = idx

        tick_count = end_idx - start_idx
        if tick_count >= 30:  # need at least 30 ticks for meaningful features
            window_prices = prices[start_idx:end_idx]

            # Extract features
            feats = extract_window_features(window_prices)
            if feats is not None:
                # Outcome: did price go up over this window?
                outcome = 1 if window_prices[-1] > window_prices[0] else 0
                window_features.append(feats)
                window_outcomes.append(outcome)
                window_times.append(current_start)

        current_start = window_end

    if not window_features:
        return np.array([]), np.array([]), np.array([])

    return (np.array(window_features, dtype=np.float32),
            np.array(window_outcomes, dtype=np.float32),
            np.array(window_times, dtype=np.int64))


def build_training_samples(window_features, window_outcomes, window_times, lookback=5):
    """
    Build training samples with context from previous windows.

    Each sample = current window's micro-features + previous windows' features
    Target = NEXT window's outcome

    Features per sample:
    - 30 micro-features from current window
    - 30 micro-features from each of `lookback` previous windows (summarized)
    - 10 cross-window context features (trends, streaks, etc.)
    Total: 30 + 30*lookback_summary + 10 context
    """
    n_windows = len(window_features)
    if n_windows < lookback + 10:
        return np.array([]), np.array([])

    n_micro = window_features.shape[1]  # 30 features per window

    features = []
    targets = []

    for i in range(lookback, n_windows - 1):
        f = []

        # 1. Current window's micro-features (30)
        f.extend(window_features[i])

        # 2. Previous windows' summarized features
        # Average of last `lookback` windows' features (30)
        prev_feats = window_features[i - lookback:i]
        f.extend(prev_feats.mean(axis=0))

        # 3. Trend of each feature over lookback (slope) (30)
        if lookback >= 3:
            x = np.arange(lookback, dtype=np.float32)
            slopes = []
            for feat_idx in range(n_micro):
                vals = prev_feats[:, feat_idx]
                if np.std(vals) > 0:
                    slope = np.polyfit(x, vals, 1)[0]
                else:
                    slope = 0
                slopes.append(slope)
            f.extend(slopes)
        else:
            f.extend([0] * n_micro)

        # 4. Cross-window context features (10)
        # Recent outcomes
        recent_outcomes = window_outcomes[i - lookback:i]
        f.append(np.mean(recent_outcomes))  # win rate of recent windows

        # Streak (use previous window's outcome to avoid lookahead)
        streak = 0
        direction = window_outcomes[i - 1] if i > 0 else 0
        for j in range(i - 1, max(i - 11, -1), -1):
            if j < 0:
                break
            if window_outcomes[j] == direction:
                streak += 1
            else:
                break
        f.append(streak / 10.0)

        # Volatility trend (are windows getting more or less volatile?)
        vol_idx = 20  # volatility is feature index 20
        recent_vols = window_features[i - lookback:i + 1, vol_idx]
        f.append(np.mean(recent_vols[-2:]) / max(np.mean(recent_vols[:-2]), 0.001))

        # Momentum trend (are returns getting bigger or smaller?)
        ret_idx = 10  # total return is feature index 10
        recent_rets = window_features[i - lookback:i + 1, ret_idx]
        f.append(np.mean(recent_rets))

        # Direction consistency (how many of last N went same direction?)
        f.append(np.std(recent_outcomes))

        # Hour of day (sin + cos encoded)
        hour = datetime.fromtimestamp(window_times[i] / 1000, tz=timezone.utc).hour
        f.append(np.sin(2 * np.pi * hour / 24))
        f.append(np.cos(2 * np.pi * hour / 24))

        # Day of week
        dow = datetime.fromtimestamp(window_times[i] / 1000, tz=timezone.utc).weekday()
        f.append(np.sin(2 * np.pi * dow / 7))

        # Current vs average trajectory (how different is this window?)
        avg_trajectory = prev_feats[:, :10].mean(axis=0)
        trajectory_diff = np.mean(np.abs(window_features[i, :10] - avg_trajectory))
        f.append(trajectory_diff)

        # Price range trend
        range_idx_up = 11  # max_up
        range_idx_dn = 12  # max_down
        recent_ranges = (window_features[i - lookback:i + 1, range_idx_up] -
                        window_features[i - lookback:i + 1, range_idx_dn])
        f.append(recent_ranges[-1] / max(np.mean(recent_ranges[:-1]), 0.001))

        features.append(f)
        targets.append(window_outcomes[i + 1])  # predict NEXT window

    return np.array(features, dtype=np.float32), np.array(targets, dtype=np.float32)


# ─── Binance API for recent data ─────────────────────────────────────

def fetch_recent_1s_data(minutes=60):
    """Fetch recent 1-second klines from Binance for current prediction."""
    all_ts = []
    all_prices = []
    # Binance 1s klines: max 1000 per request
    end_time = None
    needed = minutes * 60

    while len(all_ts) < needed:
        params = {"symbol": "BTCUSDT", "interval": "1s", "limit": 1000}
        if end_time:
            params["endTime"] = end_time
        try:
            resp = requests.get(f"{BINANCE_API}/klines", params=params, timeout=15)
            if resp.ok and resp.json():
                batch = resp.json()
                for k in batch:
                    all_ts.append(int(k[0]))
                    all_prices.append(float(k[4]))  # close
                end_time = int(batch[0][0]) - 1
                if len(batch) < 1000:
                    break
                time.sleep(0.05)
            else:
                break
        except Exception:
            break

    if not all_ts:
        return np.array([]), np.array([])

    # Sort chronologically
    pairs = sorted(zip(all_ts, all_prices))
    return np.array([p[0] for p in pairs], dtype=np.int64), \
           np.array([p[1] for p in pairs], dtype=np.float64)


# ─── Model training ──────────────────────────────────────────────────

def make_sample_weights(n_samples):
    """Recency weights: recent samples get 3x weight of oldest."""
    weights = np.linspace(1.0, 3.0, n_samples).astype(np.float32)
    return weights / weights.mean()


def train_neural_net(X, y, epochs=100):
    """Train neural net on 1s-derived features."""
    try:
        import torch
        import torch.nn as nn
    except ImportError:
        return None

    if len(X) < 200:
        return None

    mean = X.mean(axis=0)
    std = X.std(axis=0)
    std[std == 0] = 1
    X_norm = (X - mean) / std

    X_t = torch.tensor(X_norm, dtype=torch.float32)
    y_t = torch.tensor(y, dtype=torch.float32).unsqueeze(1)

    n_features = X.shape[1]

    # Bigger net for richer features
    model = nn.Sequential(
        nn.Linear(n_features, 256),
        nn.BatchNorm1d(256),
        nn.ReLU(),
        nn.Dropout(0.3),
        nn.Linear(256, 128),
        nn.BatchNorm1d(128),
        nn.ReLU(),
        nn.Dropout(0.2),
        nn.Linear(128, 64),
        nn.ReLU(),
        nn.Dropout(0.1),
        nn.Linear(64, 32),
        nn.ReLU(),
        nn.Linear(32, 1),
        nn.Sigmoid(),
    )

    optimizer = torch.optim.Adam(model.parameters(), lr=0.001, weight_decay=1e-4)

    split = int(len(X_t) * 0.8)
    X_train, X_val = X_t[:split], X_t[split:]
    y_train, y_val = y_t[:split], y_t[split:]

    sample_weights = torch.tensor(make_sample_weights(len(X_train)), dtype=torch.float32)

    batch_size = min(512, len(X_train))
    best_val_loss = float('inf')
    best_state = None
    patience = 15
    no_improve = 0
    criterion = nn.BCELoss(reduction='none')

    model.train()
    for epoch in range(epochs):
        perm = torch.randperm(len(X_train))
        X_shuf = X_train[perm]
        y_shuf = y_train[perm]
        w_shuf = sample_weights[perm]

        for start in range(0, len(X_train), batch_size):
            end = min(start + batch_size, len(X_train))
            optimizer.zero_grad()
            out = model(X_shuf[start:end])
            loss = (criterion(out, y_shuf[start:end]) * w_shuf[start:end].unsqueeze(1)).mean()
            loss.backward()
            optimizer.step()

        model.eval()
        with torch.no_grad():
            val_loss = nn.BCELoss()(model(X_val), y_val).item()
        model.train()

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
            no_improve = 0
        else:
            no_improve += 1
            if no_improve >= patience:
                break

    if best_state:
        model.load_state_dict(best_state)

    model.eval()
    with torch.no_grad():
        val_preds = model(X_val)
        val_acc = ((val_preds > 0.5).float() == y_val).float().mean().item()

    return {
        "model": model, "mean": mean, "std": std,
        "val_acc": val_acc, "n_train": len(X_train), "n_val": len(X_val),
    }


def predict_neural_net(trained, X_current):
    try:
        import torch
    except ImportError:
        return 0.5
    if trained is None:
        return 0.5
    X_norm = (X_current - trained["mean"]) / trained["std"]
    X_t = torch.tensor(X_norm.reshape(1, -1), dtype=torch.float32)
    trained["model"].eval()
    with torch.no_grad():
        return trained["model"](X_t).item()


def train_gbt(X, y):
    try:
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        return None

    if len(X) < 200:
        return None

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    split = int(len(X) * 0.8)
    X_train, X_val = X_scaled[:split], X_scaled[split:]
    y_train, y_val = y[:split], y[split:]

    train_weights = make_sample_weights(len(X_train))

    n_est = min(300, max(100, len(X_train) // 100))
    gbt = GradientBoostingClassifier(
        n_estimators=n_est, max_depth=5, learning_rate=0.03,
        subsample=0.8, min_samples_leaf=20, max_features="sqrt",
        random_state=42,
    )
    gbt.fit(X_train, y_train, sample_weight=train_weights)

    return {
        "model": gbt, "scaler": scaler,
        "val_acc": gbt.score(X_val, y_val),
        "n_train": len(X_train), "n_val": len(X_val),
    }


def predict_gbt(trained, X_current):
    if trained is None:
        return 0.5
    X_scaled = trained["scaler"].transform(X_current.reshape(1, -1))
    proba = trained["model"].predict_proba(X_scaled)[0]
    if len(proba) < 2:
        # Single-class model (training data was all one class). proba[0] is
        # the model's certainty in classes_[0]. If that class is the positive
        # class (1), the probability of UP is proba[0]; otherwise the model
        # has learned only the negative class, so probability of UP is
        # 1 - proba[0] (which equals 0.0 for the typical near-1 certainty).
        classes = getattr(trained["model"], "classes_", None)
        if classes is not None and len(classes) == 1:
            return float(proba[0]) if classes[0] == 1 else float(1.0 - proba[0])
        return 0.5
    return float(proba[1])


# ─── LSTM sequence model ─────────────────────────────────────────────

def build_sequence_samples(window_features, window_outcomes, window_times, seq_len=LSTM_SEQ_LEN):
    """
    Build sequences of consecutive windows for LSTM training.
    Each sample = seq_len consecutive window feature vectors.
    Target = outcome of the window after the sequence.
    """
    n = len(window_features)
    if n < seq_len + 10:
        return np.array([]), np.array([])

    n_feats = window_features.shape[1]
    X_seqs = []
    y_seqs = []

    for i in range(seq_len, n - 1):
        # Check windows are roughly consecutive (within 10 min gap tolerance)
        t_start = window_times[i - seq_len]
        t_end = window_times[i]
        expected_span = seq_len * WINDOW_MS
        actual_span = t_end - t_start
        if actual_span > expected_span * 2:  # skip if >2x expected (gap in data)
            continue

        X_seqs.append(window_features[i - seq_len:i])  # (seq_len, 30)
        y_seqs.append(window_outcomes[i])  # predict current window's outcome given prior context

    if not X_seqs:
        return np.array([]), np.array([])

    return np.array(X_seqs, dtype=np.float32), np.array(y_seqs, dtype=np.float32)


def train_lstm(X_seq, y, epochs=80):
    """Train LSTM on sequences of window features."""
    try:
        import torch
        import torch.nn as nn
    except ImportError:
        return None

    if len(X_seq) < 200:
        return None

    # Normalize per-feature across all sequences
    n_samples, seq_len, n_feats = X_seq.shape
    X_flat = X_seq.reshape(-1, n_feats)
    mean = X_flat.mean(axis=0)
    std = X_flat.std(axis=0)
    std[std == 0] = 1
    X_norm = ((X_seq.reshape(-1, n_feats) - mean) / std).reshape(n_samples, seq_len, n_feats)

    X_t = torch.tensor(X_norm, dtype=torch.float32)
    y_t = torch.tensor(y, dtype=torch.float32).unsqueeze(1)

    class CryptoLSTM(nn.Module):
        def __init__(self, input_size, hidden_size=64, num_layers=2, dropout=0.2):
            super().__init__()
            self.lstm = nn.LSTM(input_size, hidden_size, num_layers,
                                batch_first=True, dropout=dropout)
            self.classifier = nn.Sequential(
                nn.Linear(hidden_size, 32),
                nn.ReLU(),
                nn.Dropout(0.1),
                nn.Linear(32, 1),
                nn.Sigmoid()
            )

        def forward(self, x):
            _, (h_n, _) = self.lstm(x)
            return self.classifier(h_n[-1])

    model = CryptoLSTM(n_feats)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001, weight_decay=1e-4)

    # Chronological split
    split = int(len(X_t) * 0.8)
    X_train, X_val = X_t[:split], X_t[split:]
    y_train, y_val = y_t[:split], y_t[split:]

    sample_weights = torch.tensor(make_sample_weights(len(X_train)), dtype=torch.float32)
    batch_size = min(256, len(X_train))
    criterion = nn.BCELoss(reduction='none')
    best_val_loss = float('inf')
    best_state = None
    patience = 15
    no_improve = 0

    model.train()
    for epoch in range(epochs):
        perm = torch.randperm(len(X_train))
        X_shuf = X_train[perm]
        y_shuf = y_train[perm]
        w_shuf = sample_weights[perm]

        for start in range(0, len(X_train), batch_size):
            end = min(start + batch_size, len(X_train))
            optimizer.zero_grad()
            out = model(X_shuf[start:end])
            loss = (criterion(out, y_shuf[start:end]) * w_shuf[start:end].unsqueeze(1)).mean()
            loss.backward()
            optimizer.step()

        model.eval()
        with torch.no_grad():
            val_loss = nn.BCELoss()(model(X_val), y_val).item()
        model.train()

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
            no_improve = 0
        else:
            no_improve += 1
            if no_improve >= patience:
                break

    if best_state:
        model.load_state_dict(best_state)

    model.eval()
    with torch.no_grad():
        val_preds = model(X_val)
        val_acc = ((val_preds > 0.5).float() == y_val).float().mean().item()

    return {
        "model": model, "mean": mean, "std": std,
        "val_acc": val_acc, "seq_len": seq_len,
        "n_train": len(X_train), "n_val": len(X_val),
    }


def predict_lstm(trained, recent_window_features):
    """Predict using LSTM on the most recent seq_len window features."""
    try:
        import torch
    except ImportError:
        return 0.5
    if trained is None:
        return 0.5

    seq_len = trained["seq_len"]
    if len(recent_window_features) < seq_len:
        return 0.5

    seq = recent_window_features[-seq_len:]  # (seq_len, 30)
    seq_norm = (seq - trained["mean"]) / trained["std"]
    X_t = torch.tensor(seq_norm.reshape(1, seq_len, -1), dtype=torch.float32)

    trained["model"].eval()
    with torch.no_grad():
        return trained["model"](X_t).item()


# ─── Statistical Profile — conditional probabilities from history ─────

class StatisticalProfile:
    """
    Builds conditional probability tables from 50k+ historical windows.
    Pure statistics — no ML, just "given X, what happened historically?"
    """

    def __init__(self):
        self.stats = {}
        self.built = False

    def build(self, window_features, window_outcomes, window_times):
        """Build all statistical lookup tables from historical data."""
        n = len(window_features)
        if n < 500:
            return

        outcomes = window_outcomes.astype(int)

        # ── 1. SEQUENTIAL: P(UP | previous window was UP/DOWN) ──
        prev_up_then_up = 0
        prev_up_count = 0
        prev_down_then_up = 0
        prev_down_count = 0
        for i in range(1, n):
            if outcomes[i - 1] == 1:
                prev_up_count += 1
                if outcomes[i] == 1:
                    prev_up_then_up += 1
            else:
                prev_down_count += 1
                if outcomes[i] == 1:
                    prev_down_then_up += 1

        self.stats["p_up_after_up"] = prev_up_then_up / max(prev_up_count, 1)
        self.stats["p_up_after_down"] = prev_down_then_up / max(prev_down_count, 1)

        # ── 2. STREAK STATS: P(UP | streak of N in same direction) ──
        for streak_len in [2, 3, 4, 5]:
            # After N consecutive UPs
            count_streak_up = 0
            then_up = 0
            for i in range(streak_len, n):
                if all(outcomes[i - streak_len:i] == 1):
                    count_streak_up += 1
                    if outcomes[i] == 1:
                        then_up += 1
            self.stats[f"p_up_after_{streak_len}_ups"] = then_up / max(count_streak_up, 1)
            self.stats[f"n_after_{streak_len}_ups"] = count_streak_up

            # After N consecutive DOWNs
            count_streak_dn = 0
            then_up_dn = 0
            for i in range(streak_len, n):
                if all(outcomes[i - streak_len:i] == 0):
                    count_streak_dn += 1
                    if outcomes[i] == 1:
                        then_up_dn += 1
            self.stats[f"p_up_after_{streak_len}_downs"] = then_up_dn / max(count_streak_dn, 1)
            self.stats[f"n_after_{streak_len}_downs"] = count_streak_dn

        # ── 3. HOUR-OF-DAY: best/worst hours ──
        hour_up = {}
        hour_total = {}
        for i in range(n):
            h = datetime.fromtimestamp(window_times[i] / 1000, tz=timezone.utc).hour
            hour_total[h] = hour_total.get(h, 0) + 1
            hour_up[h] = hour_up.get(h, 0) + outcomes[i]
        self.stats["hour_p_up"] = {
            h: hour_up.get(h, 0) / max(hour_total.get(h, 1), 1)
            for h in range(24)
        }
        self.stats["hour_counts"] = hour_total

        # ── 4. FEATURE-CONDITIONAL: P(UP | feature in bucket) ──
        # For key features, bin into quintiles and compute P(UP) per bin
        feature_names = [
            (10, "total_return"),      # return of current window
            (20, "micro_volatility"),  # volatility
            (21, "uptick_ratio"),      # buying pressure
            (29, "trend_linearity"),   # R-squared
            (28, "range_efficiency"),  # closed near high/low
            (14, "time_to_peak"),      # early/late peak
        ]
        self.stats["feature_bins"] = {}
        for feat_idx, feat_name in feature_names:
            vals = window_features[:, feat_idx]
            # Create 5 bins
            percentiles = np.percentile(vals, [20, 40, 60, 80])
            # Skip features whose distribution has collapsed (constant values
            # cause every percentile to equal one another, which makes
            # np.digitize bin everything into 0 or 4 and produces a useless
            # bin_stats with degenerate (c, c) val_range).
            if len(np.unique(percentiles)) < 4:
                self.stats["feature_bins"][feat_name] = {
                    "constant": True,
                    "percentiles": percentiles.tolist(),
                    "bins": {},
                }
                continue
            bins = np.digitize(vals, percentiles)  # 0-4
            bin_stats = {}
            for b in range(5):
                mask = bins == b
                if mask.sum() > 20:
                    bin_stats[b] = {
                        "p_up": outcomes[mask].mean(),
                        "count": int(mask.sum()),
                        "val_range": (float(vals[mask].min()), float(vals[mask].max())),
                    }
            self.stats["feature_bins"][feat_name] = {
                "percentiles": percentiles.tolist(),
                "bins": bin_stats,
            }

        # ── 5. MOMENTUM CONTINUATION: already captured in feature bins above
        # (legacy dead loop removed) ──

        # ── 6. VOLATILITY REGIME: P(UP | high/low vol regime) ──
        vols = window_features[:, 20]
        vol_median = np.median(vols)
        high_vol_mask = vols[:-1] > vol_median
        low_vol_mask = vols[:-1] <= vol_median
        next_outcomes = outcomes[1:]
        self.stats["p_up_high_vol"] = next_outcomes[high_vol_mask].mean() if high_vol_mask.sum() > 50 else 0.5
        self.stats["p_up_low_vol"] = next_outcomes[low_vol_mask].mean() if low_vol_mask.sum() > 50 else 0.5
        self.stats["vol_median"] = float(vol_median)

        # ── 7. MEAN REVERSION: after big moves, what happens? ──
        returns_prev = window_features[:-1, 10]  # return of previous window
        next_out = outcomes[1:]
        big_up = returns_prev > np.percentile(returns_prev, 80)
        big_down = returns_prev < np.percentile(returns_prev, 20)
        self.stats["p_up_after_big_up"] = next_out[big_up].mean() if big_up.sum() > 50 else 0.5
        self.stats["p_up_after_big_down"] = next_out[big_down].mean() if big_down.sum() > 50 else 0.5

        # ── 8. COMBINED SIGNAL STRENGTH ──
        # P(UP | prev window UP AND high uptick ratio AND positive momentum)
        if n > 100:
            uptick = window_features[:, 21]
            uptick_med = np.median(uptick)
            returns_feat = window_features[:, 10]  # total return per window
            combined_bull = np.zeros(n - 1, dtype=bool)
            combined_bear = np.zeros(n - 1, dtype=bool)
            for i in range(n - 1):
                is_prev_up = outcomes[i] == 1
                is_high_uptick = uptick[i] > uptick_med
                is_pos_return = returns_feat[i] > 0
                combined_bull[i] = is_prev_up and is_high_uptick and is_pos_return
                combined_bear[i] = (not is_prev_up) and (not is_high_uptick) and returns_feat[i] < 0
            if combined_bull.sum() > 30:
                self.stats["p_up_combined_bull"] = next_out[combined_bull].mean()
            else:
                self.stats["p_up_combined_bull"] = 0.5
            if combined_bear.sum() > 30:
                self.stats["p_up_combined_bear"] = next_out[combined_bear].mean()
            else:
                self.stats["p_up_combined_bear"] = 0.5

        # ── 9. RECENT BIAS (last 500 windows) ──
        recent = min(500, n)
        self.stats["recent_up_rate"] = outcomes[-recent:].mean()
        self.stats["overall_up_rate"] = outcomes.mean()

        # ── 10. DAY-OF-WEEK ──
        dow_up = {}
        dow_total = {}
        for i in range(n):
            d = datetime.fromtimestamp(window_times[i] / 1000, tz=timezone.utc).weekday()
            dow_total[d] = dow_total.get(d, 0) + 1
            dow_up[d] = dow_up.get(d, 0) + outcomes[i]
        self.stats["dow_p_up"] = {
            d: dow_up.get(d, 0) / max(dow_total.get(d, 1), 1)
            for d in range(7)
        }

        self.stats["n_windows"] = n
        self.built = True

        # Print summary
        print(f"  [STATS] Built profile from {n:,} windows")
        print(f"  [STATS] P(UP|prev UP)={self.stats['p_up_after_up']:.3f} "
              f"P(UP|prev DN)={self.stats['p_up_after_down']:.3f}")
        print(f"  [STATS] P(UP|big up)={self.stats['p_up_after_big_up']:.3f} "
              f"P(UP|big dn)={self.stats['p_up_after_big_down']:.3f}")
        print(f"  [STATS] Recent UP rate={self.stats['recent_up_rate']:.3f} "
              f"Overall={self.stats['overall_up_rate']:.3f}")

    def predict(self, prev_window_features, prev_outcome, current_hour, current_dow,
                streak_count, streak_direction):
        """
        Return P(UP) for the next window based on statistical conditionals.
        Each stat contributes a weighted vote.
        """
        if not self.built:
            return 0.5, {}

        votes = []  # list of (probability, weight, reason)

        # 1. Sequential
        if prev_outcome == 1:
            p = self.stats["p_up_after_up"]
            votes.append((p, 2.0, f"prev_UP→{p:.3f}"))
        else:
            p = self.stats["p_up_after_down"]
            votes.append((p, 2.0, f"prev_DN→{p:.3f}"))

        # 2. Streak
        if streak_count >= 2:
            key = f"p_up_after_{min(streak_count, 5)}_{'ups' if streak_direction == 1 else 'downs'}"
            n_key = f"n_after_{min(streak_count, 5)}_{'ups' if streak_direction == 1 else 'downs'}"
            if key in self.stats and self.stats.get(n_key, 0) > 20:
                p = self.stats[key]
                votes.append((p, 1.5, f"streak{streak_count}→{p:.3f}"))

        # 3. Hour of day
        h_p = self.stats["hour_p_up"].get(current_hour, 0.5)
        h_count = self.stats.get("hour_counts", {}).get(current_hour, 0)
        if h_count > 100:
            votes.append((h_p, 1.0, f"hour{current_hour}→{h_p:.3f}"))

        # 4. Day of week
        d_p = self.stats["dow_p_up"].get(current_dow, 0.5)
        votes.append((d_p, 0.5, f"dow{current_dow}→{d_p:.3f}"))

        # 5. Feature conditionals (previous window's features)
        if prev_window_features is not None:
            for feat_idx, feat_name in [(10, "total_return"), (20, "micro_volatility"),
                                         (21, "uptick_ratio"), (29, "trend_linearity"),
                                         (28, "range_efficiency")]:
                fb = self.stats.get("feature_bins", {}).get(feat_name, {})
                if "percentiles" in fb:
                    val = prev_window_features[feat_idx]
                    b = int(np.digitize(val, fb["percentiles"]))
                    bin_info = fb.get("bins", {}).get(b, {})
                    if bin_info.get("count", 0) > 20:
                        p = bin_info["p_up"]
                        votes.append((p, 1.0, f"{feat_name}[{b}]→{p:.3f}"))

        # 6. Volatility regime
        if prev_window_features is not None:
            vol = prev_window_features[20]
            if vol > self.stats.get("vol_median", 0):
                votes.append((self.stats["p_up_high_vol"], 0.8, "high_vol"))
            else:
                votes.append((self.stats["p_up_low_vol"], 0.8, "low_vol"))

        # 7. Mean reversion after big moves
        if prev_window_features is not None:
            ret = prev_window_features[10]
            fb = self.stats.get("feature_bins", {}).get("total_return", {})
            if "percentiles" in fb:
                percs = fb["percentiles"]
                if ret > percs[-1]:  # top 20%
                    votes.append((self.stats["p_up_after_big_up"], 1.5, "mean_rev_big_up"))
                elif ret < percs[0]:  # bottom 20%
                    votes.append((self.stats["p_up_after_big_down"], 1.5, "mean_rev_big_dn"))

        # 8. Recent bias
        recent_rate = self.stats.get("recent_up_rate", 0.5)
        votes.append((recent_rate, 0.5, f"recent_bias→{recent_rate:.3f}"))

        # Weighted average of all votes
        if not votes:
            return 0.5, {"reason": "no_data"}

        total_w = sum(w for _, w, _ in votes)
        weighted_prob = sum(p * w for p, w, _ in votes) / total_w

        # Confidence: how much do votes agree?
        probs = [p for p, _, _ in votes]
        spread = max(probs) - min(probs)
        agreement = 1.0 - spread

        top_signals = sorted(votes, key=lambda x: abs(x[0] - 0.5) * x[1], reverse=True)[:5]

        return weighted_prob, {
            "stat_prob": round(weighted_prob, 4),
            "n_votes": len(votes),
            "agreement": round(agreement, 3),
            "top_signals": [(r, round(p, 3)) for p, _, r in top_signals],
        }


# ─── Main predictor class ────────────────────────────────────────────

class MLPredictor:
    def __init__(self, symbol="BTCUSDT"):
        self.symbol = symbol
        self.nn_model = None
        self.gbt_model = None
        self.lstm_model = None
        self.last_train_time = 0
        self.retrain_interval = 600  # 10 minutes
        self.features = None
        self.targets = None
        self.window_features = None  # per-window micro features
        self.window_outcomes = None
        self.window_times = None
        self.trained = False

    def train(self):
        now = time.time()
        if now - self.last_train_time < self.retrain_interval and self.trained:
            return None

        cache_file = features_cache_path(self.symbol)

        # Check for cached features
        cache_ok = False
        if cache_file.exists():
            cache_age = time.time() - cache_file.stat().st_mtime
            if cache_age < 3600:
                try:
                    cached = np.load(cache_file, allow_pickle=False)
                    self.window_features = cached["wf"]
                    self.window_outcomes = cached["wo"]
                    self.window_times = cached["wt"]
                    cache_ok = True
                    print(f"  [ML-{self.symbol}] Loaded cached features: "
                          f"{len(self.window_features):,} windows")
                except Exception:
                    pass

        if not cache_ok:
            print(f"  [ML-{self.symbol}] Loading raw 1-second tick data...")
            timestamps, prices = load_all_tick_data(self.symbol)
            if len(timestamps) < 10000:
                print(f"  [ML-{self.symbol}] Insufficient tick data: {len(timestamps)}")
                return None

            print(f"  [ML-{self.symbol}] Extracting micro-features from "
                  f"{len(timestamps):,} ticks...")
            t0 = time.time()
            self.window_features, self.window_outcomes, self.window_times = \
                build_windows_and_features(timestamps, prices)
            del timestamps, prices
            t1 = time.time()
            print(f"  [ML-{self.symbol}] Extracted features for "
                  f"{len(self.window_features):,} windows in {t1-t0:.1f}s")

            try:
                np.savez(cache_file,
                         wf=self.window_features,
                         wo=self.window_outcomes,
                         wt=self.window_times)
            except Exception:
                pass

        if self.window_features is None or len(self.window_features) < 100:
            return None

        # Build flat training samples (for NN + GBT)
        print(f"  [ML-{self.symbol}] Building training samples...")
        X, y = build_training_samples(
            self.window_features, self.window_outcomes, self.window_times, lookback=5)

        if len(X) < 200:
            print(f"  [ML-{self.symbol}] Insufficient samples: {len(X)}")
            return None

        self.features = X
        self.targets = y

        n_features = X.shape[1]
        print(f"  [ML-{self.symbol}] Training on {len(X):,} samples x {n_features} features "
              f"(up rate: {y.mean():.1%})...")

        # Train all three models
        self.nn_model = train_neural_net(X, y, epochs=100)
        self.gbt_model = train_gbt(X, y)

        # LSTM on raw window sequences
        X_seq, y_seq = build_sequence_samples(
            self.window_features, self.window_outcomes, self.window_times)
        if len(X_seq) >= 200:
            self.lstm_model = train_lstm(X_seq, y_seq, epochs=80)
        else:
            self.lstm_model = None

        self.last_train_time = now
        self.trained = True

        nn_acc = self.nn_model["val_acc"] if self.nn_model else 0
        gbt_acc = self.gbt_model["val_acc"] if self.gbt_model else 0
        lstm_acc = self.lstm_model["val_acc"] if self.lstm_model else 0
        n_train = self.nn_model.get('n_train', 0) if self.nn_model else 0
        print(f"  [ML-{self.symbol}] Training complete: NN={nn_acc:.1%} GBT={gbt_acc:.1%} "
              f"LSTM={lstm_acc:.1%} ({n_train:,} train)")

        return nn_acc, gbt_acc, lstm_acc

    def predict(self):
        if self.features is None or len(self.features) == 0:
            return 0.5, {"nn_prob": 0.5, "gbt_prob": 0.5, "lstm_prob": 0.5,
                         "ensemble": 0.5, "confidence": "no_data"}

        X_current = self.features[-1]
        nn_prob = predict_neural_net(self.nn_model, X_current)
        gbt_prob = predict_gbt(self.gbt_model, X_current)

        # LSTM prediction using recent window features
        lstm_prob = 0.5
        if self.lstm_model and self.window_features is not None:
            lstm_prob = predict_lstm(self.lstm_model, self.window_features)

        nn_acc = self.nn_model["val_acc"] if self.nn_model else 0.5
        gbt_acc = self.gbt_model["val_acc"] if self.gbt_model else 0.5
        lstm_acc = self.lstm_model["val_acc"] if self.lstm_model else 0.5

        # Weighted ensemble (accuracy-squared weighting)
        nn_w = nn_acc ** 2
        gbt_w = gbt_acc ** 2
        lstm_w = lstm_acc ** 2 if self.lstm_model else 0
        total_w = nn_w + gbt_w + lstm_w
        ensemble = ((nn_prob * nn_w + gbt_prob * gbt_w + lstm_prob * lstm_w) / total_w
                     if total_w > 0 else 0.5)

        # Confidence from 3-model agreement
        probs = [nn_prob, gbt_prob]
        if self.lstm_model:
            probs.append(lstm_prob)
        spread = max(probs) - min(probs)
        if spread < 0.15 and max(nn_acc, gbt_acc, lstm_acc) > 0.53:
            confidence = "high"
        elif spread < 0.30:
            confidence = "medium"
        else:
            confidence = "low"

        return ensemble, {
            "nn_prob": round(nn_prob, 4), "gbt_prob": round(gbt_prob, 4),
            "lstm_prob": round(lstm_prob, 4), "ensemble": round(ensemble, 4),
            "nn_acc": round(nn_acc, 4), "gbt_acc": round(gbt_acc, 4),
            "lstm_acc": round(lstm_acc, 4),
            "confidence": confidence,
            "n_train_samples": len(self.features),
        }


# ─── Multi-coin predictor registry ──────────────────────────────────

_predictors = {}  # coin -> MLPredictor


def get_ml_prediction(coin="btc"):
    symbol = COIN_SYMBOLS.get(coin, "BTCUSDT")

    # Check if pickle data exists for this coin
    pickle_files = list(CACHE_DIR.glob(f"{symbol}_1s_*.pkl"))
    if not pickle_files:
        return 0.5, {"confidence": "no_data", "reason": f"no tick data for {symbol}"}

    if coin not in _predictors:
        _predictors[coin] = MLPredictor(symbol)

    predictor = _predictors[coin]
    try:
        result = predictor.train()
        prob, info = predictor.predict()
        if result:
            info["train_nn_acc"] = round(result[0], 4)
            info["train_gbt_acc"] = round(result[1], 4)
            info["train_lstm_acc"] = round(result[2], 4)
        return prob, info
    except Exception as e:
        return 0.5, {"error": str(e), "confidence": "error"}

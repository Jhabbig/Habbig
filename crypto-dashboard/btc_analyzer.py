#!/usr/bin/env python3
"""
Multi-Asset 5-Minute Window Analyzer
Fetches 1-second kline data from Binance for BTC, ETH, SOL, DOGE, XRP,
splits into 5-minute windows, trains neural net ensembles, and generates
a tabbed HTML dashboard with live predictions.
"""

import requests
import time
import json
import asyncio
import aiohttp
import hashlib
import hmac as _hmac
import pickle
import math
import tempfile
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

import numpy as np


def _pickle_hmac_key() -> bytes:
    """Derive a stable HMAC key for pickle integrity verification."""
    secret = os.environ.get("PICKLE_HMAC_SECRET", "")
    if not secret:
        key_file = Path(__file__).parent / ".secret_key"
        if key_file.exists():
            secret = key_file.read_bytes().hex()
        else:
            secret = "polymarket-cache-default"
    return hashlib.sha256(secret.encode() if isinstance(secret, str) else secret).digest()


def safe_pickle_dump(data, filepath):
    """Pickle dump with HMAC integrity signature."""
    raw = pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL)
    sig = _hmac.new(_pickle_hmac_key(), raw, hashlib.sha256).digest()
    with open(filepath, "wb") as f:
        f.write(sig + raw)


def safe_pickle_load(filepath):
    """Pickle load with HMAC integrity verification. Falls back to unsigned for migration."""
    with open(filepath, "rb") as f:
        content = f.read()
    # Try HMAC-signed format first (32-byte SHA256 prefix)
    if len(content) > 32:
        sig = content[:32]
        raw = content[32:]
        expected = _hmac.new(_pickle_hmac_key(), raw, hashlib.sha256).digest()
        if _hmac.compare_digest(sig, expected):
            return pickle.loads(raw)
    # Reject unsigned pickle — refuse to deserialize untrusted data.
    # If a legacy unsigned cache file exists, delete it and return None
    # so the caller regenerates the data and saves it with HMAC.
    print(f"[SECURITY] Rejecting unsigned pickle file: {filepath} — delete and regenerate")
    return None

# ─── GPU acceleration via CuPy (falls back to NumPy on CPU) ─────────
try:
    import cupy as cp
    xp = cp        # GPU array module
    GPU = True
    print(f"[GPU] CuPy {cp.__version__} — using {cp.cuda.runtime.getDeviceCount()} GPU(s)")
except Exception:
    xp = np        # fallback: plain numpy
    GPU = False
    print("[CPU] CuPy not available — running on CPU")


def to_gpu(a):
    """Move a numpy array to GPU (no-op if CPU mode)."""
    return xp.asarray(a) if GPU else a


def to_cpu(a):
    """Move an array back to CPU numpy (no-op if already numpy)."""
    return cp.asnumpy(a) if GPU else a


# ─── Config ───────────────────────────────────────────────────────────
ASSETS = {
    "BTC":  {"symbol": "BTCUSDT",  "name": "Bitcoin"},
    "ETH":  {"symbol": "ETHUSDT",  "name": "Ethereum"},
    "SOL":  {"symbol": "SOLUSDT",  "name": "Solana"},
    "DOGE": {"symbol": "DOGEUSDT", "name": "Dogecoin"},
    "XRP":  {"symbol": "XRPUSDT",  "name": "XRP"},
}
INTERVAL = "1s"
WINDOW_MINUTES = 5
WINDOW_SECONDS = WINDOW_MINUTES * 60
BINANCE_KLINE_URL = "https://api.binance.com/api/v3/klines"
MAX_PER_REQUEST = 1000
CACHE_DIR = Path(__file__).parent / "cache"
RATE_LIMIT_PAUSE = 0.02  # ~50 req/s, well within Binance 1200/min limit
HISTORY_DAYS = 180


# ═══════════════════════════════════════════════════════════════════════
# DATA FETCHING
# ═══════════════════════════════════════════════════════════════════════

def fetch_klines(symbol, start_ms, end_ms):
    """Fetch klines using parallel async requests for speed."""
    CONCURRENT = 40  # parallel requests
    CHUNK_MS = MAX_PER_REQUEST * 1000  # each request covers 1000 seconds

    # Build list of time chunks
    chunks = []
    current = start_ms
    while current < end_ms:
        chunk_end = min(current + CHUNK_MS, end_ms)
        chunks.append((current, chunk_end))
        current = chunk_end

    total_chunks = len(chunks)
    results = [None] * total_chunks
    completed = [0]

    async def fetch_chunk(session, sem, idx, s, e):
        params = {
            "symbol": symbol, "interval": INTERVAL,
            "startTime": s, "endTime": e, "limit": MAX_PER_REQUEST,
        }
        async with sem:
            for attempt in range(5):
                try:
                    async with session.get(BINANCE_KLINE_URL, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status == 429:
                            wait = int(resp.headers.get("Retry-After", "10"))
                            print(f"    Rate limited, waiting {wait}s...")
                            await asyncio.sleep(wait)
                            continue
                        resp.raise_for_status()
                        data = await resp.json()
                        results[idx] = data
                        completed[0] += 1
                        if completed[0] % 200 == 0 or completed[0] == total_chunks:
                            pct = completed[0] / total_chunks * 100
                            print(f"    {pct:.1f}% ({completed[0]}/{total_chunks} chunks)")
                        return
                except Exception as e_err:
                    if attempt == 4:
                        print(f"    Chunk {idx} failed after 5 attempts: {e_err}")
                        results[idx] = []
                        return
                    await asyncio.sleep(2)

    async def run_all():
        sem = asyncio.Semaphore(CONCURRENT)
        async with aiohttp.ClientSession() as session:
            tasks = [fetch_chunk(session, sem, i, s, e) for i, (s, e) in enumerate(chunks)]
            await asyncio.gather(*tasks)

    # Run the async event loop (works from sync context)
    try:
        loop = asyncio.get_running_loop()
        # Already in async context — use nest_asyncio or thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            pool.submit(lambda: asyncio.run(run_all())).result()
    except RuntimeError:
        asyncio.run(run_all())

    # Combine all chunks in order
    all_klines = []
    for r in results:
        if r:
            all_klines.extend(r)

    return all_klines


def load_or_fetch(symbol, days=HISTORY_DAYS):
    CACHE_DIR.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    end_dt = now.replace(second=0, microsecond=0)
    start_dt = end_dt - timedelta(days=days)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    # Prefer pickle cache (5-10x smaller, 10x faster to load)
    pickle_file = CACHE_DIR / f"{symbol}_1s_{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}.pkl"

    def _normalize_pickle(data):
        """Handle both (ts, price) and (ts, open, high, low, close, vol) formats."""
        if data and len(data[0]) > 2:
            return [(r[0], float(r[4])) for r in data]  # use close price
        return data

    # Check for pickle cache first
    if pickle_file.exists():
        print(f"  Loading cached {symbol} (pickle)...")
        try:
            data = _normalize_pickle(safe_pickle_load(pickle_file))
            print(f"    {len(data):,} candles from pickle cache.")
            return data, start_dt, end_dt
        except (pickle.UnpicklingError, EOFError, ValueError, TypeError) as e:
            print(f"  WARNING: Corrupted pickle {pickle_file}: {e}, will re-fetch")

    # Check for recent pickle files
    min_candles = days * 86400 * 0.8
    for ef in sorted(CACHE_DIR.glob(f"{symbol}_1s_*.pkl"), key=lambda p: p.stat().st_mtime, reverse=True):
        age_hours = (time.time() - ef.stat().st_mtime) / 3600
        if age_hours < 48:
            print(f"  Loading recent pickle {ef.name} ({age_hours:.0f}h old)...")
            try:
                data = _normalize_pickle(safe_pickle_load(ef))
                if len(data) >= min_candles:
                    print(f"    {len(data):,} candles from pickle cache.")
                    return data, start_dt, end_dt
            except (pickle.UnpicklingError, EOFError, ValueError, TypeError) as e:
                print(f"  WARNING: Corrupted pickle {ef.name}: {e}, skipping")

    # Check for JSON cache and convert to pickle
    json_file = CACHE_DIR / f"{symbol}_1s_{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}.json"
    json_candidates = [json_file] if json_file.exists() else []
    if not json_candidates:
        json_candidates = sorted(CACHE_DIR.glob(f"{symbol}_1s_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)

    for jf in json_candidates:
        age_hours = (time.time() - jf.stat().st_mtime) / 3600
        if age_hours < 48:
            file_size = jf.stat().st_size
            estimated_candles = file_size / 170
            if estimated_candles >= min_candles:
                print(f"  Loading JSON cache {jf.name} and converting to pickle...")
                with open(jf) as f:
                    klines = json.load(f)
                # Parse immediately and save as pickle (much smaller)
                data = [(k[0], float(k[4])) for k in klines]
                data.sort(key=lambda x: x[0])
                del klines  # free the huge JSON immediately
                import gc; gc.collect()
                safe_pickle_dump(data, pickle_file)
                print(f"    {len(data):,} candles. Pickle saved ({pickle_file.stat().st_size/1e6:.0f}MB vs {jf.stat().st_size/1e6:.0f}MB JSON)")
                return data, start_dt, end_dt

    # Nothing cached — fetch from Binance
    print(f"  Fetching {days}d of {symbol} 1s data...")
    klines = fetch_klines(symbol, start_ms, end_ms)
    print(f"    Done: {len(klines):,} candles.")
    # Parse and save as pickle directly (skip JSON entirely)
    data = [(k[0], float(k[4])) for k in klines]
    data.sort(key=lambda x: x[0])
    del klines
    import gc; gc.collect()
    safe_pickle_dump(data, pickle_file)
    print(f"    Saved pickle cache ({pickle_file.stat().st_size/1e6:.0f}MB)")
    return data, start_dt, end_dt


def parse_klines(raw):
    """Parse raw klines OR pass through already-parsed data."""
    if isinstance(raw, list) and raw and isinstance(raw[0], tuple):
        return raw  # already parsed (from pickle)
    data = [(k[0], float(k[4])) for k in raw]
    data.sort(key=lambda x: x[0])
    return data


# ═══════════════════════════════════════════════════════════════════════
# WINDOW ANALYSIS
# ═══════════════════════════════════════════════════════════════════════

def analyze_windows(data):
    if not data:
        return []

    first_ts = data[0][0]
    window_ms = WINDOW_SECONDS * 1000
    align_ts = first_ts - (first_ts % window_ms)
    windows = []
    data_idx = 0
    n = len(data)
    current_window_start = align_ts

    while data_idx < n:
        window_end = current_window_start + window_ms
        window_data = []
        while data_idx < n and data[data_idx][0] < window_end:
            window_data.append(data[data_idx])
            data_idx += 1

        if len(window_data) < 10:
            current_window_start = window_end
            continue

        baseline = window_data[0][1]
        window_start_dt = datetime.fromtimestamp(current_window_start / 1000, tz=timezone.utc)
        deltas = [(ts, price - baseline) for ts, price in window_data]

        # Last cross: last time delta changed sign in either direction
        last_cross_sec = None
        last_cross_direction = None
        for i in range(1, len(deltas)):
            prev_d = deltas[i - 1][1]
            curr_d = deltas[i][1]
            if (prev_d >= 0 and curr_d < 0):
                last_cross_sec = (deltas[i][0] - current_window_start) / 1000
                last_cross_direction = "negative"
            elif (prev_d < 0 and curr_d >= 0):
                last_cross_sec = (deltas[i][0] - current_window_start) / 1000
                last_cross_direction = "positive"

        max_positive = max(d for _, d in deltas)
        max_negative = min(d for _, d in deltas)
        avg_delta = sum(d for _, d in deltas) / len(deltas)
        end_delta = deltas[-1][1]

        positive_count = sum(1 for _, d in deltas if d >= 0)
        negative_count = sum(1 for _, d in deltas if d < 0)
        pos_deltas = [d for _, d in deltas if d > 0]
        neg_deltas = [d for _, d in deltas if d < 0]
        avg_pos_magnitude = sum(pos_deltas) / len(pos_deltas) if pos_deltas else 0
        avg_neg_magnitude = sum(neg_deltas) / len(neg_deltas) if neg_deltas else 0

        # RSI-like metric for the window (based on recent closes in the window)
        gains, losses = [], []
        for i in range(1, len(window_data)):
            diff = window_data[i][1] - window_data[i-1][1]
            if diff > 0:
                gains.append(diff)
            else:
                losses.append(abs(diff))
        avg_gain = sum(gains) / max(len(gains), 1)
        avg_loss = sum(losses) / max(len(losses), 1)
        rsi = 100 - (100 / (1 + avg_gain / (avg_loss + 1e-10)))

        # Number of zero-crossings (choppiness)
        crossings = 0
        for i in range(1, len(deltas)):
            if (deltas[i-1][1] >= 0) != (deltas[i][1] >= 0):
                crossings += 1

        windows.append({
            "start": window_start_dt,
            "baseline": baseline,
            "last_cross_sec": last_cross_sec,
            "last_cross_direction": last_cross_direction,
            "max_positive": max_positive,
            "max_negative": max_negative,
            "avg_delta": avg_delta,
            "end_delta": end_delta,
            "avg_pos_magnitude": avg_pos_magnitude,
            "avg_neg_magnitude": avg_neg_magnitude,
            "positive_pct": positive_count / len(deltas) * 100,
            "negative_pct": negative_count / len(deltas) * 100,
            "candle_count": len(window_data),
            "rsi": rsi,
            "crossings": crossings,
        })

        current_window_start = window_end

    return windows


def compute_summary(windows):
    if not windows:
        return {
            "total_windows": 0, "avg_last_cross_sec": None, "median_last_cross_sec": None,
            "windows_with_cross": 0, "windows_that_went_negative": 0,
            "windows_that_went_positive": 0, "windows_ended_positive": 0,
            "windows_ended_negative": 0, "avg_max_positive": 0, "avg_max_negative": 0,
            "avg_pos_magnitude": 0, "avg_neg_magnitude": 0, "avg_end_delta": 0,
            "avg_rsi": 50, "avg_crossings": 0,
        }
    crosses = [w["last_cross_sec"] for w in windows if w["last_cross_sec"] is not None]
    wn = sum(1 for w in windows if w["max_negative"] < 0)
    wp = sum(1 for w in windows if w["max_positive"] > 0)
    ep = sum(1 for w in windows if w["end_delta"] > 0)
    en = sum(1 for w in windows if w["end_delta"] < 0)
    n = len(windows)

    return {
        "total_windows": n,
        "avg_last_cross_sec": sum(crosses) / len(crosses) if crosses else None,
        "median_last_cross_sec": float(np.median(crosses)) if crosses else None,
        "windows_with_cross": len(crosses),
        "windows_that_went_negative": wn,
        "windows_that_went_positive": wp,
        "windows_ended_positive": ep,
        "windows_ended_negative": en,
        "avg_max_positive": sum(w["max_positive"] for w in windows) / n,
        "avg_max_negative": sum(w["max_negative"] for w in windows) / n,
        "avg_pos_magnitude": sum(w["avg_pos_magnitude"] for w in windows) / n,
        "avg_neg_magnitude": sum(w["avg_neg_magnitude"] for w in windows) / n,
        "avg_end_delta": sum(w["end_delta"] for w in windows) / n,
        "avg_rsi": sum(w["rsi"] for w in windows) / n,
        "avg_crossings": sum(w["crossings"] for w in windows) / n,
    }


def compute_volatility(windows, lookback_hours=24):
    """
    Compute volatility score from the last `lookback_hours` of 5-min windows.

    Method: standard deviation of percentage returns (close-to-close)
    across the lookback window.

    Thresholds:
      < 2% std dev → NOT VOLATILE
      2-5% std dev → BORDERLINE
      > 5% std dev → VOLATILE

    Returns dict with score, label, color, and breakdown.
    """
    # 24 hours = 288 five-minute windows
    windows_per_hour = 60 // WINDOW_MINUTES
    lookback_n = lookback_hours * windows_per_hour
    recent = windows[-lookback_n:] if len(windows) >= lookback_n else windows

    if len(recent) < 10:
        return {"std_pct": 0, "label": "UNKNOWN", "color": "muted", "details": {}}

    # Percentage returns between consecutive windows
    prices = [w["baseline"] + w["end_delta"] for w in recent]  # closing price
    returns_pct = []
    for i in range(1, len(prices)):
        if prices[i-1] > 0:
            ret = ((prices[i] - prices[i-1]) / prices[i-1]) * 100
            returns_pct.append(ret)

    if not returns_pct:
        return {"std_pct": 0, "label": "UNKNOWN", "color": "muted", "details": {}}

    returns_arr = np.array(returns_pct)
    std_pct = float(np.std(returns_arr))
    mean_ret = float(np.mean(returns_arr))
    max_ret = float(np.max(returns_arr))
    min_ret = float(np.min(returns_arr))

    # Annualized vol (for reference): std * sqrt(windows_per_year)
    # 5-min windows per year ≈ 105,120
    annualized = std_pct * math.sqrt(105120)

    # Classification
    if std_pct >= 5.0:
        label = "VOLATILE"
        color = "negative"
    elif std_pct >= 2.0:
        label = "BORDERLINE"
        color = "yellow"
    else:
        label = "NOT VOLATILE"
        color = "positive"

    # Trend: is volatility increasing or decreasing? Need at least 2 returns
    # to split into halves; otherwise fall back to a single std (no trend).
    if len(returns_arr) >= 2:
        half = len(returns_arr) // 2
        first_half_std = float(np.std(returns_arr[:half])) if half > 0 else float(np.std(returns_arr))
        second_half_std = float(np.std(returns_arr[half:]))
    else:
        first_half_std = float(np.std(returns_arr)) if len(returns_arr) > 0 else 0.0
        second_half_std = first_half_std
    vol_trend = "INCREASING" if second_half_std > first_half_std * 1.2 else (
        "DECREASING" if second_half_std < first_half_std * 0.8 else "STABLE")

    return {
        "std_pct": round(std_pct, 3),
        "label": label,
        "color": color,
        "mean_return_pct": round(mean_ret, 4),
        "max_return_pct": round(max_ret, 3),
        "min_return_pct": round(min_ret, 3),
        "annualized_vol": round(annualized, 1),
        "vol_trend": vol_trend,
        "first_half_std": round(first_half_std, 3),
        "second_half_std": round(second_half_std, 3),
        "n_windows": len(recent),
        "lookback_hours": lookback_hours,
    }


def compute_per_second_velocity(data, windows):
    """
    Compute per-second gain/loss velocity metrics from raw 1s data.

    Returns dict with:
    - avg_gain_per_sec: average $ gained per second during upward moves
    - avg_loss_per_sec: average $ lost per second during downward moves
    - avg_run_duration_up: average seconds of continuous upward movement
    - avg_run_duration_down: average seconds of continuous downward movement
    - avg_velocity_after_cross_pos: avg $/sec in the 30s after crossing positive
    - avg_velocity_after_cross_neg: avg $/sec in the 30s after crossing negative
    - momentum_decay: how quickly velocity fades (ratio of first 15s vs next 15s after cross)
    - best_entry_window_sec: optimal seconds after cross to enter (max avg move)
    - avg_time_to_peak: avg seconds from window start to max positive
    - avg_time_to_trough: avg seconds from window start to max negative
    """
    if len(data) < 100:
        return {}

    # Use last 24h of data for speed (86400 seconds)
    data = data[-172800:] if len(data) > 172800 else data  # last 48h
    windows = windows[-576:] if len(windows) > 576 else windows  # 576 = 48h of 5min windows

    # ── Per-second deltas across all data ──
    gains_per_sec = []  # positive price changes
    losses_per_sec = []  # negative price changes
    run_up_durations = []
    run_down_durations = []

    current_run = 0
    current_direction = 0  # 1=up, -1=down, 0=flat

    for i in range(1, len(data)):
        delta = data[i][1] - data[i-1][1]
        if delta > 0:
            gains_per_sec.append(delta)
            if current_direction == 1:
                current_run += 1
            else:
                if current_direction == -1 and current_run > 0:
                    run_down_durations.append(current_run)
                current_run = 1
                current_direction = 1
        elif delta < 0:
            losses_per_sec.append(abs(delta))
            if current_direction == -1:
                current_run += 1
            else:
                if current_direction == 1 and current_run > 0:
                    run_up_durations.append(current_run)
                current_run = 1
                current_direction = -1
        # delta == 0: continue current run

    if current_direction == 1 and current_run > 0:
        run_up_durations.append(current_run)
    elif current_direction == -1 and current_run > 0:
        run_down_durations.append(current_run)

    avg_gain = sum(gains_per_sec) / len(gains_per_sec) if gains_per_sec else 0
    avg_loss = sum(losses_per_sec) / len(losses_per_sec) if losses_per_sec else 0
    avg_run_up = sum(run_up_durations) / len(run_up_durations) if run_up_durations else 0
    avg_run_down = sum(run_down_durations) / len(run_down_durations) if run_down_durations else 0

    # ── Post-cross velocity analysis ──
    # For each window, find cross events and measure velocity in the 30s after
    window_ms = WINDOW_SECONDS * 1000
    data_dict = {ts: price for ts, price in data}

    velocities_after_cross_pos = []
    velocities_after_cross_neg = []
    first_15s_moves = []
    second_15s_moves = []
    time_to_peaks = []
    time_to_troughs = []
    cumulative_by_second = [[] for _ in range(WINDOW_SECONDS)]  # $/move at each second offset

    for w in windows:
        w_start_ms = int(w["start"].timestamp() * 1000)
        w_end_ms = w_start_ms + window_ms
        baseline = w["baseline"]

        # Collect window prices by second offset (direct lookup, no iteration)
        w_prices = {}
        for sec in range(WINDOW_SECONDS):
            ts = w_start_ms + sec * 1000
            if ts in data_dict:
                w_prices[sec] = data_dict[ts]

        if len(w_prices) < 30:
            continue

        # Track cumulative delta at each second for optimal entry analysis
        for sec, price in w_prices.items():
            if sec < WINDOW_SECONDS:
                cumulative_by_second[sec].append(price - baseline)

        # Time to peak/trough
        peak_sec = max(w_prices.keys(), key=lambda s: w_prices[s])
        trough_sec = min(w_prices.keys(), key=lambda s: w_prices[s])
        time_to_peaks.append(peak_sec)
        time_to_troughs.append(trough_sec)

        # Find cross events in this window
        sorted_secs = sorted(w_prices.keys())
        for i in range(1, len(sorted_secs)):
            s_prev = sorted_secs[i-1]
            s_curr = sorted_secs[i]
            d_prev = w_prices[s_prev] - baseline
            d_curr = w_prices[s_curr] - baseline

            crossed_pos = d_prev < 0 and d_curr >= 0
            crossed_neg = d_prev >= 0 and d_curr < 0

            if crossed_pos or crossed_neg:
                cross_price = w_prices[s_curr]
                # Measure velocity over next 30s
                future_prices = [(s, w_prices[s]) for s in sorted_secs if s > s_curr and s <= s_curr + 30]
                if len(future_prices) >= 5:
                    move_30s = future_prices[-1][1] - cross_price
                    velocity = move_30s / len(future_prices)

                    if crossed_pos:
                        velocities_after_cross_pos.append(velocity)
                    else:
                        velocities_after_cross_neg.append(velocity)

                    # Momentum decay: first 15s vs second 15s
                    first_half = [p for s, p in future_prices if s <= s_curr + 15]
                    second_half = [p for s, p in future_prices if s > s_curr + 15]
                    if first_half and second_half:
                        move_first = first_half[-1] - cross_price
                        move_second = second_half[-1] - first_half[-1]
                        first_15s_moves.append(abs(move_first))
                        second_15s_moves.append(abs(move_second))

    # ── Optimal entry timing ──
    # Find the second offset where absolute cumulative move is maximized (on average)
    avg_abs_by_second = []
    for sec in range(WINDOW_SECONDS):
        if cumulative_by_second[sec]:
            avg_abs_by_second.append((sec, np.mean(np.abs(cumulative_by_second[sec]))))
        else:
            avg_abs_by_second.append((sec, 0))

    best_entry_sec = max(avg_abs_by_second, key=lambda x: x[1])[0] if avg_abs_by_second else 150

    # Momentum decay ratio
    avg_first_15 = sum(first_15s_moves) / len(first_15s_moves) if first_15s_moves else 0
    avg_second_15 = sum(second_15s_moves) / len(second_15s_moves) if second_15s_moves else 0
    momentum_decay = avg_second_15 / avg_first_15 if avg_first_15 > 0 else 1.0

    return {
        "avg_gain_per_sec": avg_gain,
        "avg_loss_per_sec": avg_loss,
        "net_per_sec": avg_gain - avg_loss,
        "gain_loss_ratio": avg_gain / avg_loss if avg_loss > 0 else 0,
        "avg_run_duration_up": avg_run_up,
        "avg_run_duration_down": avg_run_down,
        "avg_velocity_after_cross_pos": sum(velocities_after_cross_pos) / len(velocities_after_cross_pos) if velocities_after_cross_pos else 0,
        "avg_velocity_after_cross_neg": sum(velocities_after_cross_neg) / len(velocities_after_cross_neg) if velocities_after_cross_neg else 0,
        "momentum_decay_ratio": round(momentum_decay, 3),
        "best_entry_sec": best_entry_sec,
        "avg_time_to_peak_sec": sum(time_to_peaks) / len(time_to_peaks) if time_to_peaks else 0,
        "avg_time_to_trough_sec": sum(time_to_troughs) / len(time_to_troughs) if time_to_troughs else 0,
        "total_gain_samples": len(gains_per_sec),
        "total_loss_samples": len(losses_per_sec),
        "pct_seconds_gaining": len(gains_per_sec) / max(len(gains_per_sec) + len(losses_per_sec), 1) * 100,
    }


# ═══════════════════════════════════════════════════════════════════════
# NEURAL NETWORK
# ═══════════════════════════════════════════════════════════════════════

# Use float32 on GPU for ~50x speedup (tensor cores), float64 on CPU
DTYPE = np.float32 if GPU else np.float64

def relu(x):
    return xp.maximum(0, x)

def relu_deriv(x):
    return (x > 0).astype(DTYPE)

def sigmoid(x):
    return 1.0 / (1.0 + xp.exp(-xp.clip(x, -500, 500)))


class AdamOptimizer:
    def __init__(self, lr=0.001, beta1=0.9, beta2=0.999, eps=1e-8):
        self.lr, self.beta1, self.beta2, self.eps = lr, beta1, beta2, eps
        self.m, self.v, self.t = {}, {}, 0

    def step(self, params, grads):
        self.t += 1
        for key in params:
            if key not in self.m:
                self.m[key] = xp.zeros_like(params[key])
                self.v[key] = xp.zeros_like(params[key])
            self.m[key] = self.beta1 * self.m[key] + (1 - self.beta1) * grads[key]
            self.v[key] = self.beta2 * self.v[key] + (1 - self.beta2) * (grads[key] ** 2)
            m_hat = self.m[key] / (1 - self.beta1 ** self.t)
            v_hat = self.v[key] / (1 - self.beta2 ** self.t)
            params[key] -= self.lr * m_hat / (xp.sqrt(v_hat) + self.eps)


class WindowNeuralNet:
    LOOKBACK = 96

    def __init__(self):
        self.params = {}
        self.feature_mean = None
        self.feature_std = None
        self.target_mean = 0.0
        self.target_std = 1.0
        self.trained = False

    def _extract_features(self, windows, idx):
        start = max(0, idx - self.LOOKBACK)
        recent = windows[start:idx]
        if len(recent) < 6:
            return None

        target_hour = windows[idx]["start"].hour if idx < len(windows) else 0
        target_dow = windows[idx]["start"].weekday() if idx < len(windows) else 0
        features = []

        end_deltas = np.array([w["end_delta"] for w in recent])
        max_pos = np.array([w["max_positive"] for w in recent])
        max_neg = np.array([w["max_negative"] for w in recent])
        vols = max_pos - max_neg
        avg_deltas = np.array([w["avg_delta"] for w in recent])
        pct_pos = np.array([w["positive_pct"] for w in recent])
        rsis = np.array([w["rsi"] for w in recent])
        cross_counts = np.array([w["crossings"] for w in recent])

        # Last 6 deltas + vols
        for arr in [end_deltas, vols]:
            last6 = arr[-6:]
            pad = np.zeros(6)
            pad[-len(last6):] = last6
            features.extend(pad.tolist())

        # Momentum
        if len(end_deltas) > 0:
            weights = np.exp(np.linspace(-2, 0, len(end_deltas)))
            ws = weights.sum()
            if ws > 0:
                weights /= ws
                features.append(float(np.dot(weights, end_deltas)))
            else:
                features.append(0.0)
        else:
            features.append(0.0)
        features.append(float(np.mean(end_deltas[-3:])))
        features.append(float(np.mean(end_deltas[-6:])))
        features.append(float(np.mean(end_deltas[-12:])) if len(end_deltas) >= 12 else float(np.mean(end_deltas)))
        features.append(float(np.mean(end_deltas)))

        # Trend
        if len(end_deltas) >= 3:
            coeffs = np.polyfit(np.arange(len(end_deltas), dtype=float), end_deltas, 1)
            features.extend([float(coeffs[0]), float(coeffs[1])])
        else:
            features.extend([0.0, 0.0])

        # Volatility
        features.append(float(np.mean(vols)))
        features.append(float(np.std(vols)))
        features.append(float(np.mean(vols[-3:])))
        features.append(float(vols[-1]))
        features.append(float(np.mean(vols[-6:])) / (float(np.mean(vols)) + 1e-8))

        # Mean reversion
        features.append(float(np.sum(end_deltas[-6:])))
        features.append(float(np.sum(end_deltas[-12:])) if len(end_deltas) >= 12 else float(np.sum(end_deltas)))

        # Streak
        streak = 0
        if len(end_deltas) >= 2:
            last_dir = 1 if end_deltas[-1] >= 0 else -1
            for d in reversed(end_deltas):
                if (1 if d >= 0 else -1) == last_dir:
                    streak += 1
                else:
                    break
            features.append(float(streak * last_dir))
            features.append(float(np.mean(end_deltas[-streak:])))
        else:
            features.extend([0.0, 0.0])

        # Asymmetry
        features.extend([float(np.mean(max_pos)), float(np.mean(max_neg)),
                         float(np.mean(max_pos[-3:])), float(np.mean(max_neg[-3:])),
                         float(np.mean(avg_deltas)), float(np.mean(pct_pos))])

        # Time encoding
        features.extend([math.sin(2*math.pi*target_hour/24), math.cos(2*math.pi*target_hour/24),
                         math.sin(2*math.pi*target_dow/7), math.cos(2*math.pi*target_dow/7)])

        # Price change %
        features.append((recent[-1]["baseline"] - recent[0]["baseline"]) / (recent[0]["baseline"] + 1e-8) * 100)

        # Last cross time
        crosses = [w["last_cross_sec"] for w in recent if w["last_cross_sec"] is not None]
        features.append(float(np.mean(crosses)) if crosses else 150.0)

        # RSI features
        features.extend([float(np.mean(rsis[-6:])), float(np.mean(rsis)), float(rsis[-1])])

        # Choppiness
        features.extend([float(np.mean(cross_counts[-6:])), float(np.mean(cross_counts))])

        # Win rates
        features.append(float(np.mean([1 if d >= 0 else 0 for d in end_deltas[-12:]])))
        features.append(float(np.mean([1 if d >= 0 else 0 for d in end_deltas])))

        # Acceleration
        if len(end_deltas) >= 12:
            features.append(float(np.mean(end_deltas[-6:])) - float(np.mean(end_deltas[-12:-6])))
        else:
            features.append(0.0)

        # Vol trend
        if len(vols) >= 12:
            features.append(float(np.mean(vols[-6:])) - float(np.mean(vols[-12:-6])))
        else:
            features.append(0.0)

        # Range ratio
        features.append(float(np.mean(vols[-6:])) / (float(np.mean(vols)) + 1e-8) if len(vols) >= 6 else 1.0)

        # Autocorrelation
        if len(end_deltas) >= 4:
            ac = float(np.corrcoef(end_deltas[:-1], end_deltas[1:])[0, 1])
            features.append(ac if not np.isnan(ac) else 0.0)
        else:
            features.append(0.0)

        # Drawdown / rally
        cumsum = np.cumsum(end_deltas)
        features.append(float(np.min(cumsum - np.maximum.accumulate(cumsum))))
        features.append(float(np.max(cumsum - np.minimum.accumulate(cumsum))))

        # ── New features for improved accuracy ──

        # Bollinger band position: where is current delta relative to recent range
        if len(end_deltas) >= 12:
            bb_mean = float(np.mean(end_deltas[-12:]))
            bb_std = float(np.std(end_deltas[-12:])) + 1e-8
            features.append((float(end_deltas[-1]) - bb_mean) / bb_std)  # z-score
        else:
            features.append(0.0)

        # Rate of change of volatility (vol acceleration)
        if len(vols) >= 12:
            vol_roc = float(np.mean(vols[-3:])) - float(np.mean(vols[-12:-3]))
            features.append(vol_roc)
        else:
            features.append(0.0)

        # Skewness of recent deltas
        if len(end_deltas) >= 12:
            d_mean = float(np.mean(end_deltas[-12:]))
            d_std = float(np.std(end_deltas[-12:])) + 1e-8
            skew = float(np.mean(((end_deltas[-12:] - d_mean) / d_std) ** 3))
            features.append(skew)
        else:
            features.append(0.0)

        # Kurtosis of recent deltas (tail risk)
        if len(end_deltas) >= 12:
            k_mean = float(np.mean(end_deltas[-12:]))
            k_std = float(np.std(end_deltas[-12:])) + 1e-8
            kurt = float(np.mean(((end_deltas[-12:] - k_mean) / k_std) ** 4)) - 3.0
            features.append(kurt)
        else:
            features.append(0.0)

        # RSI momentum (change in RSI)
        if len(rsis) >= 6:
            features.append(float(rsis[-1]) - float(np.mean(rsis[-6:])))
        else:
            features.append(0.0)

        # Consecutive positive/negative pct change
        if len(pct_pos) >= 6:
            features.append(float(np.mean(pct_pos[-3:])) - float(np.mean(pct_pos[-6:])))
        else:
            features.append(0.0)

        # Price distance from session high/low (mean reversion signal)
        if len(recent) >= 12:
            highs = [w["baseline"] + w["max_positive"] for w in recent[-12:]]
            lows = [w["baseline"] + w["max_negative"] for w in recent[-12:]]
            curr = recent[-1]["baseline"]
            price_range = max(highs) - min(lows) + 1e-8
            features.append((curr - min(lows)) / price_range)  # 0=at low, 1=at high
        else:
            features.append(0.5)

        # Lag-2 autocorrelation (require >=10 samples for statistical reliability)
        if len(end_deltas) >= 10:
            ac2 = float(np.corrcoef(end_deltas[:-2], end_deltas[2:])[0, 1])
            features.append(ac2 if not np.isnan(ac2) else 0.0)
        else:
            features.append(0.0)

        return np.array(features, dtype=DTYPE)

    def _init_weights(self, n_input, seed=42):
        # Generate weights on CPU with a local RNG for thread-safe reproducibility
        rng = np.random.RandomState(seed)
        h1, h2, h3, h4 = 64, 48, 32, 16
        dt = DTYPE
        self.params = {
            "W1": to_gpu(rng.randn(n_input, h1).astype(dt) * np.sqrt(2.0/n_input)), "b1": xp.zeros((1,h1), dtype=dt),
            "W2": to_gpu(rng.randn(h1, h2).astype(dt) * np.sqrt(2.0/h1)), "b2": xp.zeros((1,h2), dtype=dt),
            "W3": to_gpu(rng.randn(h2, h3).astype(dt) * np.sqrt(2.0/h2)), "b3": xp.zeros((1,h3), dtype=dt),
            "W4": to_gpu(rng.randn(h3, h4).astype(dt) * np.sqrt(2.0/h3)), "b4": xp.zeros((1,h4), dtype=dt),
            "W_reg": to_gpu(rng.randn(h4, 1).astype(dt) * np.sqrt(2.0/h4)), "b_reg": xp.zeros((1,1), dtype=dt),
            "W_cls": to_gpu(np.random.randn(h4, 1).astype(dt) * np.sqrt(2.0/h4)), "b_cls": xp.zeros((1,1), dtype=dt),
            "bn1_g": xp.ones((1,h1), dtype=dt), "bn1_b": xp.zeros((1,h1), dtype=dt),
            "bn2_g": xp.ones((1,h2), dtype=dt), "bn2_b": xp.zeros((1,h2), dtype=dt),
            "bn3_g": xp.ones((1,h3), dtype=dt), "bn3_b": xp.zeros((1,h3), dtype=dt),
            "bn4_g": xp.ones((1,h4), dtype=dt), "bn4_b": xp.zeros((1,h4), dtype=dt),
        }
        self.bn_running = {
            "mu1": xp.zeros((1,h1), dtype=dt), "var1": xp.ones((1,h1), dtype=dt),
            "mu2": xp.zeros((1,h2), dtype=dt), "var2": xp.ones((1,h2), dtype=dt),
            "mu3": xp.zeros((1,h3), dtype=dt), "var3": xp.ones((1,h3), dtype=dt),
            "mu4": xp.zeros((1,h4), dtype=dt), "var4": xp.ones((1,h4), dtype=dt),
        }
        self.dropout_rate = 0.25

    def _batchnorm(self, x, layer, training=True):
        g = self.params[f"bn{layer}_g"]; b = self.params[f"bn{layer}_b"]
        if training and x.shape[0] > 1:
            mu = x.mean(axis=0, keepdims=True)
            var = x.var(axis=0, keepdims=True) + 1e-8
            # Update running stats
            self.bn_running[f"mu{layer}"] = 0.9 * self.bn_running[f"mu{layer}"] + 0.1 * mu
            self.bn_running[f"var{layer}"] = 0.9 * self.bn_running[f"var{layer}"] + 0.1 * var
        else:
            mu = self.bn_running[f"mu{layer}"]
            var = self.bn_running[f"var{layer}"] + 1e-8
        xn = (x - mu) / xp.sqrt(var)
        return g * xn + b, mu, var, xn

    def _dropout(self, x, training=True):
        if training and self.dropout_rate > 0:
            mask = (xp.random.rand(*x.shape) > self.dropout_rate).astype(DTYPE)
            return x * mask / (1 - self.dropout_rate), mask
        return x, xp.ones_like(x)

    def _forward(self, X, training=True):
        z1 = X @ self.params["W1"] + self.params["b1"]
        bn1, mu1, var1, xn1 = self._batchnorm(z1, 1, training)
        a1 = relu(bn1)
        a1d, mask1 = self._dropout(a1, training)

        z2 = a1d @ self.params["W2"] + self.params["b2"]
        bn2, mu2, var2, xn2 = self._batchnorm(z2, 2, training)
        a2 = relu(bn2)
        a2d, mask2 = self._dropout(a2, training)

        z3 = a2d @ self.params["W3"] + self.params["b3"]
        bn3, mu3, var3, xn3 = self._batchnorm(z3, 3, training)
        a3 = relu(bn3)
        a3d, mask3 = self._dropout(a3, training)

        z4 = a3d @ self.params["W4"] + self.params["b4"]
        bn4, mu4, var4, xn4 = self._batchnorm(z4, 4, training)
        a4 = relu(bn4)

        reg_out = a4 @ self.params["W_reg"] + self.params["b_reg"]
        cls_out = sigmoid(a4 @ self.params["W_cls"] + self.params["b_cls"])
        return reg_out, cls_out, {
            "X":X, "z1":z1,"bn1":bn1,"a1":a1,"a1d":a1d,"mask1":mask1,
            "z2":z2,"bn2":bn2,"a2":a2,"a2d":a2d,"mask2":mask2,
            "z3":z3,"bn3":bn3,"a3":a3,"a3d":a3d,"mask3":mask3,
            "z4":z4,"bn4":bn4,"a4":a4,
            "reg_out":reg_out,"cls_out":cls_out,
        }

    def _forward_cpu(self, X):
        """CPU-only forward pass for single-sample inference (no dropout)."""
        p = self.params; bn = self.bn_running
        z1 = X @ p["W1"] + p["b1"]
        xn1 = (z1 - bn["mu1"]) / np.sqrt(bn["var1"] + 1e-8)
        a1 = np.maximum(0, p["bn1_g"] * xn1 + p["bn1_b"])

        z2 = a1 @ p["W2"] + p["b2"]
        xn2 = (z2 - bn["mu2"]) / np.sqrt(bn["var2"] + 1e-8)
        a2 = np.maximum(0, p["bn2_g"] * xn2 + p["bn2_b"])

        z3 = a2 @ p["W3"] + p["b3"]
        xn3 = (z3 - bn["mu3"]) / np.sqrt(bn["var3"] + 1e-8)
        a3 = np.maximum(0, p["bn3_g"] * xn3 + p["bn3_b"])

        z4 = a3 @ p["W4"] + p["b4"]
        xn4 = (z4 - bn["mu4"]) / np.sqrt(bn["var4"] + 1e-8)
        a4 = np.maximum(0, p["bn4_g"] * xn4 + p["bn4_b"])

        reg_out = a4 @ p["W_reg"] + p["b_reg"]
        cls_out = 1.0 / (1.0 + np.exp(-np.clip(a4 @ p["W_cls"] + p["b_cls"], -500, 500)))
        return reg_out, cls_out, None

    def _backward(self, cache, y_reg, y_cls):
        m = cache["X"].shape[0]; grads = {}

        # Output gradients
        d_reg = (cache["reg_out"] - y_reg) / m
        grads["W_reg"] = 0.5 * (cache["a4"].T @ d_reg); grads["b_reg"] = 0.5 * xp.sum(d_reg, axis=0, keepdims=True)
        d_from_reg = d_reg @ self.params["W_reg"].T

        cls_out = xp.clip(cache["cls_out"], 1e-7, 1-1e-7)
        d_cls = (cls_out - y_cls) / m
        grads["W_cls"] = 0.5 * (cache["a4"].T @ d_cls); grads["b_cls"] = 0.5 * xp.sum(d_cls, axis=0, keepdims=True)

        # Layer 4 (no dropout on last hidden)
        d_a4 = 0.5 * d_from_reg + 0.5 * (d_cls @ self.params["W_cls"].T)
        d_z4 = d_a4 * relu_deriv(cache["bn4"])
        grads["W4"] = cache["a3d"].T @ d_z4; grads["b4"] = xp.sum(d_z4, axis=0, keepdims=True)
        grads["bn4_g"] = xp.zeros_like(self.params["bn4_g"]); grads["bn4_b"] = xp.zeros_like(self.params["bn4_b"])

        # Layer 3
        d_a3d = (d_z4 @ self.params["W4"].T)
        d_a3 = d_a3d * cache["mask3"]
        d_z3 = d_a3 * relu_deriv(cache["bn3"])
        grads["W3"] = cache["a2d"].T @ d_z3; grads["b3"] = xp.sum(d_z3, axis=0, keepdims=True)
        grads["bn3_g"] = xp.zeros_like(self.params["bn3_g"]); grads["bn3_b"] = xp.zeros_like(self.params["bn3_b"])

        # Layer 2
        d_a2d = (d_z3 @ self.params["W3"].T)
        d_a2 = d_a2d * cache["mask2"]
        d_z2 = d_a2 * relu_deriv(cache["bn2"])
        grads["W2"] = cache["a1d"].T @ d_z2; grads["b2"] = xp.sum(d_z2, axis=0, keepdims=True)
        grads["bn2_g"] = xp.zeros_like(self.params["bn2_g"]); grads["bn2_b"] = xp.zeros_like(self.params["bn2_b"])

        # Layer 1
        d_a1d = (d_z2 @ self.params["W2"].T)
        d_a1 = d_a1d * cache["mask1"]
        d_z1 = d_a1 * relu_deriv(cache["bn1"])
        grads["W1"] = cache["X"].T @ d_z1; grads["b1"] = xp.sum(d_z1, axis=0, keepdims=True)
        grads["bn1_g"] = xp.zeros_like(self.params["bn1_g"]); grads["bn1_b"] = xp.zeros_like(self.params["bn1_b"])

        # L2 regularization
        for k in ["W1","W2","W3","W4","W_reg","W_cls"]:
            grads[k] += 3e-3 * self.params[k]
        return grads

    def _build_dataset(self, windows):
        X, yr, yc = [], [], []
        for i in range(self.LOOKBACK, len(windows)):
            f = self._extract_features(windows, i)
            if f is None: continue
            X.append(f); yr.append(windows[i]["end_delta"]); yc.append(1.0 if windows[i]["end_delta"]>=0 else 0.0)
        return np.array(X, dtype=DTYPE), np.array(yr, dtype=DTYPE).reshape(-1,1), np.array(yc, dtype=DTYPE).reshape(-1,1)

    def train(self, windows, epochs=3000, lr=0.001, noise=0.10, seed=42, verbose=True):
        X, y_reg, y_cls = self._build_dataset(windows)
        n = X.shape[0]
        self.feature_mean = X.mean(axis=0); self.feature_std = X.std(axis=0)+1e-8
        X = (X - self.feature_mean) / self.feature_std
        self.target_mean = float(y_reg.mean()); self.target_std = float(y_reg.std())+1e-8
        y_reg_n = (y_reg - self.target_mean) / self.target_std

        si = int(n * 0.85)

        # Move training data to GPU
        Xt, Xv = to_gpu(X[:si]), to_gpu(X[si:])
        yrt, yrv = to_gpu(y_reg_n[:si]), to_gpu(y_reg_n[si:])
        yct, ycv = to_gpu(y_cls[:si]), to_gpu(y_cls[si:])
        yrv_raw = to_gpu(y_reg[si:])

        # Keep CPU copies of feature stats for predict()
        self.feature_mean = self.feature_mean  # stays numpy
        self.feature_std = self.feature_std

        if verbose:
            print(f"    Samples: {n}, Train: {si}, Val: {n-si}, Device: {'GPU' if GPU else 'CPU'}")

        self._init_weights(X.shape[1], seed=seed)
        opt = AdamOptimizer(lr=lr)
        best_vl, best_p, patience, pc = float("inf"), None, 500, 0
        bs = min(256, Xt.shape[0])
        lr_reductions = 0
        current_lr = lr

        train_rng = np.random.RandomState(seed)
        for epoch in range(epochs):
            train_rng.seed(seed + epoch)  # reproducible per-epoch seeding
            perm = train_rng.permutation(int(Xt.shape[0]))
            for b in range(0, int(Xt.shape[0]), bs):
                batch_end = min(b + bs, int(Xt.shape[0]))
                idx = perm[b:batch_end]
                Xb = Xt[idx] + xp.asarray(train_rng.randn(len(idx), int(Xt.shape[1])).astype(DTYPE)) * noise
                _, _, cache = self._forward(Xb, training=True)
                grads = self._backward(cache, yrt[idx], yct[idx])
                opt.step(self.params, grads)

            # Validation (no dropout)
            vr, vc, _ = self._forward(Xv, training=False)
            val_mse = float(xp.mean((vr-yrv)**2))
            vc_c = xp.clip(vc, 1e-7, 1-1e-7)
            val_bce = float(-xp.mean(ycv*xp.log(vc_c)+(1-ycv)*xp.log(1-vc_c)))
            vl = 0.4*val_mse + 0.6*val_bce  # weight classification more heavily

            if vl < best_vl - 1e-5:
                best_vl = vl
                best_p = {k:v.copy() for k,v in self.params.items()}
                best_bn = {k:v.copy() for k,v in self.bn_running.items()}
                pc = 0
            else:
                pc += 1

            # LR reduction on plateau (reduce 2x after 200 epochs of no improvement)
            if pc == 200 and lr_reductions < 3:
                current_lr *= 0.5
                opt.lr = current_lr
                lr_reductions += 1
                if verbose: print(f"      LR reduced to {current_lr:.6f}")

            if verbose and (epoch+1) % 200 == 0:
                da = float(xp.mean((vc>=0.5).astype(DTYPE)==ycv))
                mae = float(xp.mean(xp.abs(vr*self.target_std+self.target_mean-yrv_raw)))
                print(f"      Epoch {epoch+1:>4d} | val_loss={vl:.5f} | dir_acc={da:.3f} | MAE=${mae:.2f} | lr={current_lr:.5f}")
            if pc >= patience:
                if verbose: print(f"      Early stop at epoch {epoch+1}")
                break

        if best_p:
            self.params = best_p
            self.bn_running = best_bn
        # Move params back to CPU for inference compatibility
        if GPU:
            self.params = {k: to_cpu(v) for k, v in self.params.items()}
            self.bn_running = {k: to_cpu(v) for k, v in self.bn_running.items()}
        self.trained = True

    def save_state(self):
        """Return serializable state dict for persistence."""
        return {
            "params": {k: v.tolist() for k, v in self.params.items()},
            "bn_running": {k: v.tolist() for k, v in self.bn_running.items()},
            "feature_mean": self.feature_mean.tolist() if self.feature_mean is not None else None,
            "feature_std": self.feature_std.tolist() if self.feature_std is not None else None,
            "target_mean": self.target_mean,
            "target_std": self.target_std,
            "trained": self.trained,
            "dropout_rate": self.dropout_rate,
        }

    def load_state(self, state):
        """Restore from serialized state dict."""
        self.params = {k: np.array(v, dtype=np.float64) for k, v in state["params"].items()}
        self.bn_running = {k: np.array(v, dtype=np.float64) for k, v in state["bn_running"].items()}
        self.feature_mean = np.array(state["feature_mean"], dtype=np.float64) if state["feature_mean"] is not None else None
        self.feature_std = np.array(state["feature_std"], dtype=np.float64) if state["feature_std"] is not None else None
        self.target_mean = state["target_mean"]
        self.target_std = state["target_std"]
        self.trained = state["trained"]
        self.dropout_rate = state.get("dropout_rate", 0.25)

    def predict(self, windows, idx):
        f = self._extract_features(windows, idx)
        if f is None or not self.trained: return None
        X = (f.reshape(1,-1) - self.feature_mean) / self.feature_std
        # Predict on CPU (params already moved to CPU after training)
        reg, cls, _ = self._forward_cpu(X)
        delta = float(reg[0,0]) * self.target_std + self.target_mean
        prob = float(cls[0,0])
        start = max(0, idx - self.LOOKBACK)
        recent = windows[start:idx]
        vols = [w["max_positive"]-w["max_negative"] for w in recent]
        vr = (np.mean(vols[-6:])/(np.mean(vols)+1e-8)) if len(vols)>=6 else 1.0
        rmp = np.mean([w["max_positive"] for w in recent]) if recent else 0
        rmn = np.mean([w["max_negative"] for w in recent]) if recent else 0
        crosses = [w["last_cross_sec"] for w in recent if w["last_cross_sec"] is not None]
        eds = [w["end_delta"] for w in recent]
        # Guard against empty `eds` — np.linspace(-1,0,0) is empty, sum()==0,
        # and the resulting division would emit RuntimeWarning + NaN momentum.
        if eds:
            wts = np.exp(np.linspace(-1, 0, len(eds)))
            wt_sum = wts.sum()
            if wt_sum > 0:
                wts /= wt_sum
                momentum_val = float(np.dot(wts, eds))
            else:
                momentum_val = 0.0
        else:
            momentum_val = 0.0
        return {
            "pred_end_delta": round(delta, 2),
            "pred_direction": "positive" if prob >= 0.5 else "negative",
            "pred_prob_positive": round(prob, 3),
            "pred_max_up": round(float(rmp * (0.85+0.3*vr)), 2),
            "pred_max_down": round(float(rmn * (0.85+0.3*vr)), 2),
            "pred_cross_sec": round(float(np.mean(crosses)), 1) if crosses else None,
            "confidence": round(abs(prob-0.5)*2, 2),
            "vol_regime": "HIGH" if vr>1.3 else ("LOW" if vr<0.7 else "NORMAL"),
            "momentum": round(momentum_val, 2),
            "avg_rsi": round(float(np.mean([w["rsi"] for w in recent[-6:]])), 1) if recent else 50,
        }


class EnsemblePredictor:
    CONFIGS = [
        {"seed": 42,   "lr": 0.001,  "epochs": 6000,  "noise": 0.10},
        {"seed": 123,  "lr": 0.002,  "epochs": 6000,  "noise": 0.12},
        {"seed": 777,  "lr": 0.0008, "epochs": 8000,  "noise": 0.08},
        {"seed": 2024, "lr": 0.0015, "epochs": 6000,  "noise": 0.15},
        {"seed": 999,  "lr": 0.001,  "epochs": 7000,  "noise": 0.10},
        {"seed": 314,  "lr": 0.0005, "epochs": 10000, "noise": 0.08},
        {"seed": 555,  "lr": 0.0012, "epochs": 6000,  "noise": 0.12},
    ]

    # Human-readable names for each model style
    MODEL_NAMES = [
        "Balanced",        # seed=42, standard lr/noise
        "Aggressive",      # seed=123, high lr, high noise
        "Conservative",    # seed=777, low lr, low noise, long train
        "Noisy",           # seed=2024, mid lr, highest noise
        "Steady",          # seed=999, standard lr/noise, longer train
        "Patient",         # seed=314, very low lr, long train, low noise
        "Adaptive",        # seed=555, mid lr, high noise
    ]

    def __init__(self):
        self.models = []
        self.model_info = []  # per-model metadata for dashboard

    def train_all(self, windows, verbose=True):
        self.model_weights = []
        self.model_info = []
        for i, cfg in enumerate(self.CONFIGS):
            if verbose: print(f"    Model {i+1}/{len(self.CONFIGS)} (seed={cfg['seed']})")
            m = WindowNeuralNet()
            m.train(windows, epochs=cfg["epochs"], lr=cfg["lr"], noise=cfg["noise"],
                    seed=cfg["seed"], verbose=verbose)
            self.models.append(m)

        # Compute per-model validation accuracy for weighted ensemble
        lb = WindowNeuralNet.LOOKBACK
        n_total = len(windows) - lb
        si = lb + int(n_total * 0.85)
        raw_accs = []
        for m in self.models:
            correct = 0; total = 0
            for i in range(si, len(windows)):
                p = m.predict(windows, i)
                if p is None: continue
                actual = "positive" if windows[i]["end_delta"] >= 0 else "negative"
                if p["pred_direction"] == actual: correct += 1
                total += 1
            acc = correct / total if total else 0.5
            raw_accs.append(acc)
            # Weight = accuracy squared (reward better models more)
            self.model_weights.append(acc ** 2)
            if verbose: print(f"      Model val_acc={acc:.3f}, weight={acc**2:.4f}")

        # Normalize weights
        total_w = sum(self.model_weights)
        self.model_weights = [w / total_w for w in self.model_weights]

        # Store model info for dashboard
        for i, cfg in enumerate(self.CONFIGS):
            self.model_info.append({
                "name": self.MODEL_NAMES[i],
                "seed": cfg["seed"],
                "lr": cfg["lr"],
                "epochs": cfg["epochs"],
                "noise": cfg["noise"],
                "accuracy": round(raw_accs[i], 4),
                "weight": round(self.model_weights[i], 4),
            })

    def predict(self, windows, idx):
        preds = [m.predict(windows, idx) for m in self.models]
        valid = [(i, p) for i, p in enumerate(preds) if p is not None]
        if not valid: return None

        # Weighted average using per-model accuracy
        weights = [self.model_weights[i] for i, _ in valid]
        w_sum = sum(weights)
        weights = [w / w_sum for w in weights]

        avg_delta = sum(w * p["pred_end_delta"] for w, (_, p) in zip(weights, valid))
        avg_prob = sum(w * p["pred_prob_positive"] for w, (_, p) in zip(weights, valid))
        dirs = [1 if p["pred_prob_positive"]>=0.5 else 0 for _, p in valid]
        agree = max(sum(dirs), len(dirs)-sum(dirs)) / len(valid)
        # Per-model breakdown
        model_details = []
        for i, p in enumerate(preds):
            if p is None:
                continue
            info = self.model_info[i] if i < len(self.model_info) else {}
            model_details.append({
                "name": info.get("name", f"Model {i+1}"),
                "direction": p["pred_direction"],
                "prob": p["pred_prob_positive"],
                "delta": p["pred_end_delta"],
                "accuracy": info.get("accuracy", 0),
                "weight": info.get("weight", 0),
            })

        return {
            "pred_end_delta": round(avg_delta, 2),
            "pred_direction": "positive" if avg_prob>=0.5 else "negative",
            "pred_prob_positive": round(avg_prob, 3),
            "pred_max_up": valid[0][1]["pred_max_up"],
            "pred_max_down": valid[0][1]["pred_max_down"],
            "pred_cross_sec": valid[0][1]["pred_cross_sec"],
            "confidence": round((agree-0.5)*2, 2),
            "vol_regime": valid[0][1]["vol_regime"],
            "momentum": valid[0][1]["momentum"],
            "avg_rsi": valid[0][1]["avg_rsi"],
            "ensemble_agreement": f"{sum(dirs)}/{len(dirs)}",
            "individual_probs": [p["pred_prob_positive"] for _, p in valid],
            "model_details": model_details,
        }

    def backtest(self, windows):
        lb = WindowNeuralNet.LOOKBACK
        n_total = len(windows) - lb
        si = lb + int(n_total * 0.85)
        correct = 0; total = 0; errors = []; hc_c = 0; hc_t = 0
        for i in range(si, len(windows)):
            p = self.predict(windows, i)
            if p is None: continue
            actual_dir = "positive" if windows[i]["end_delta"]>=0 else "negative"
            dc = p["pred_direction"] == actual_dir
            if dc: correct += 1
            total += 1
            if p["confidence"] >= 0.4:
                hc_t += 1
                if dc: hc_c += 1
            errors.append(abs(p["pred_end_delta"] - windows[i]["end_delta"]))
        return {
            "total": total,
            "dir_acc": correct/total if total else 0,
            "hc_acc": hc_c/hc_t if hc_t else 0,
            "hc_count": hc_t,
            "mae": float(np.mean(errors)) if errors else 0,
            "median_ae": float(np.median(errors)) if errors else 0,
        }

    def predict_next(self, windows):
        if not windows:
            return None
        last = windows[-1]
        ns = last["start"] + timedelta(minutes=WINDOW_MINUTES)
        dummy = {"start": ns, "end_delta":0, "max_positive":0, "max_negative":0,
                 "avg_delta":0, "baseline":last["baseline"], "positive_pct":50,
                 "last_cross_sec":None, "last_cross_direction":None,
                 "rsi":50, "crossings":0}
        p = self.predict(windows + [dummy], len(windows))
        if p: p["window_start"] = ns
        return p

    def save_to_file(self, filepath):
        """Save entire ensemble to a JSON file (atomic write)."""
        state = {
            "model_states": [m.save_state() for m in self.models],
            "model_weights": self.model_weights,
            "model_info": self.model_info,
            "configs": self.CONFIGS,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(filepath) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f)
            os.replace(tmp, filepath)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def load_from_file(cls, filepath):
        """Load a pre-trained ensemble from file. Returns None if file missing or corrupt."""
        fp = Path(filepath)
        if not fp.exists():
            return None
        try:
            with open(fp) as f:
                state = json.load(f)
            ens = cls()
            for ms in state["model_states"]:
                m = WindowNeuralNet()
                m.load_state(ms)
                ens.models.append(m)
            ens.model_weights = state["model_weights"]
            ens.model_info = state.get("model_info", [])
            return ens
        except Exception as e:
            print(f"  Warning: Could not load saved model from {filepath}: {e}")
            return None

    def predict_current_and_recent(self, windows):
        """Predict current (next) + last 3 completed windows."""
        results = []
        # Last 3 completed
        for i in range(max(0, len(windows)-3), len(windows)):
            p = self.predict(windows, i)
            if p:
                p["window_start"] = windows[i]["start"]
                p["actual_end_delta"] = windows[i]["end_delta"]
                p["actual_direction"] = "positive" if windows[i]["end_delta"]>=0 else "negative"
                p["is_current"] = False
                results.append(p)
        # Next (current/upcoming)
        nxt = self.predict_next(windows)
        if nxt:
            nxt["actual_end_delta"] = None
            nxt["actual_direction"] = None
            nxt["is_current"] = True
            results.append(nxt)
        return results


# ═══════════════════════════════════════════════════════════════════════
# HTML DASHBOARD
# ═══════════════════════════════════════════════════════════════════════

def _render_model_panel(res):
    """Render the ensemble model breakdown panel."""
    model_info = res.get("model_info")
    preds = res.get("predictions")
    if not model_info:
        return ""

    # Get the current prediction's model details (last pred = current)
    current_pred = None
    if preds:
        for p in preds:
            if p.get("is_current"):
                current_pred = p
                break
        if not current_pred:
            current_pred = preds[-1] if preds else None

    model_details = current_pred.get("model_details", []) if current_pred else []

    # Build model rows
    model_rows = ""
    for i, mi in enumerate(model_info):
        # Find this model's current prediction
        detail = model_details[i] if i < len(model_details) else None
        if detail:
            dir_class = "positive" if detail["direction"] == "positive" else "negative"
            dir_str = detail["direction"].upper()
            prob_str = f'{detail["prob"]:.1%}'
            delta_str = f'${detail["delta"]:+,.2f}'
        else:
            dir_class = "muted"
            dir_str = "—"
            prob_str = "—"
            delta_str = "—"

        acc_class = "positive" if mi["accuracy"] >= 0.53 else ("yellow" if mi["accuracy"] >= 0.50 else "negative")
        weight_pct = mi["weight"] * 100
        # Bar width proportional to weight (max weight ~20% for 7 models)
        bar_width = min(weight_pct * 5, 100)

        model_rows += f"""<tr>
          <td style="font-weight:600;">{mi["name"]}</td>
          <td class="{dir_class}" style="font-weight:600;">{dir_str}</td>
          <td>{prob_str}</td>
          <td>{delta_str}</td>
          <td class="{acc_class}">{mi["accuracy"]*100:.1f}%</td>
          <td style="width:120px;">
            <div style="display:flex;align-items:center;gap:6px;">
              <div style="background:var(--blue);height:8px;border-radius:4px;width:{bar_width}%;opacity:0.7;"></div>
              <span style="font-size:0.75em;color:var(--muted);">{weight_pct:.0f}%</span>
            </div>
          </td>
          <td style="font-size:0.7em;color:var(--muted);">lr={mi["lr"]}, noise={mi["noise"]}, {mi["epochs"]}ep</td>
        </tr>"""

    return f"""
          <details style="margin-top:16px;margin-bottom:4px;">
            <summary style="cursor:pointer;color:var(--blue);font-size:0.95em;font-weight:600;margin-bottom:12px;">
              Ensemble Model Breakdown (7 models) <i class="info-tip" data-tip="7 neural networks with different training settings vote on each prediction. Models with higher test accuracy get more weight in the final decision.">?</i>
            </summary>
            <div class="table-container">
              <table>
                <thead><tr>
                  <th>Model</th><th>Vote</th><th>Prob +</th><th>Delta</th>
                  <th>Test Acc</th><th>Weight</th><th>Config</th>
                </tr></thead>
                <tbody>{model_rows}</tbody>
              </table>
            </div>
          </details>"""


def generate_dashboard(all_results):
    """Generate multi-asset tabbed HTML dashboard."""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    tab_buttons = ""
    tab_contents = ""
    chart_data_all = {}  # {ticker: {24h: [...], 7d: [...]}}

    for idx, (ticker, res) in enumerate(all_results.items()):
        active = "active" if idx == 0 else ""
        info = ASSETS[ticker]
        s = res["summary"]
        bt = res["backtest"]
        preds = res["predictions"]  # last 3 + current
        windows = res["windows"]

        # Prediction cards
        pred_cards = ""
        if not preds:
            pred_cards = '<div class="pred-card" style="border-color:var(--yellow);"><div style="padding:12px;text-align:center;color:var(--yellow);font-weight:600;">Models training on GPU... predictions will appear shortly.</div></div>'
        for p in (preds or []):
            is_cur = p["is_current"]
            border = "var(--green)" if is_cur else "var(--border)"
            label = "UPCOMING" if is_cur else "COMPLETED"
            label_color = "var(--green)" if is_cur else "var(--muted)"
            dir_class = "positive" if p["pred_direction"] == "positive" else "negative"
            prob = p["pred_prob_positive"]
            prob_str = f"{prob:.0%}" if prob >= 0.5 else f"{(1-prob):.0%}"
            actual_str = ""
            if p["actual_end_delta"] is not None:
                ac = p["actual_end_delta"]
                ac_class = "positive" if ac >= 0 else "negative"
                correct = p["pred_direction"] == p["actual_direction"]
                tick = "check" if correct else "x"
                tick_color = "var(--green)" if correct else "var(--red)"
                actual_str = f'<div class="detail">Actual: <span class="{ac_class}">${ac:+,.2f}</span> {"&#10003;" if correct else "&#10007;"}</div>'

            cross_str = f'{p["pred_cross_sec"]:.0f}s' if p["pred_cross_sec"] else "—"
            time_str = p["window_start"].strftime("%H:%M") if p.get("window_start") else "—"

            pred_cards += f"""
            <div class="pred-card" style="border-color:{border};">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                <span style="font-size:1.1em;font-weight:700;">{time_str} UTC</span>
                <span style="font-size:0.7em;color:{label_color};font-weight:600;letter-spacing:0.05em;">{label}</span>
              </div>
              <div style="display:flex;gap:16px;flex-wrap:wrap;">
                <div><span class="mini-label">Direction <i class="info-tip" data-tip="Predicted price direction for this 5-min window. POSITIVE = price expected to rise, NEGATIVE = expected to fall.">?</i></span><br><span class="value-sm {dir_class}">{p["pred_direction"].upper()}</span></div>
                <div><span class="mini-label">Delta <i class="info-tip" data-tip="Predicted price change in dollars from start to end of the 5-minute window.">?</i></span><br><span class="value-sm">${p["pred_end_delta"]:+,.2f}</span></div>
                <div><span class="mini-label">Prob <i class="info-tip" data-tip="Model probability that price ends higher. Above 50% = bullish, below 50% = bearish.">?</i></span><br><span class="value-sm">{prob_str}</span></div>
                <div><span class="mini-label">Confidence <i class="info-tip" data-tip="How sure the model is. 0% = coin flip, 100% = very confident. Based on ensemble agreement.">?</i></span><br><span class="value-sm">{p["confidence"]*100:.0f}%</span></div>
                <div><span class="mini-label">Range <i class="info-tip" data-tip="Expected max price swing up and down within the window, based on recent volatility.">?</i></span><br><span class="value-sm"><span class="positive">${p["pred_max_up"]:+,.2f}</span> / <span class="negative">${p["pred_max_down"]:+,.2f}</span></span></div>
                <div><span class="mini-label">Last Cross <i class="info-tip" data-tip="Predicted second when price crosses back through the opening price. Earlier = choppier market.">?</i></span><br><span class="value-sm">{cross_str}</span></div>
                <div><span class="mini-label">Vol <i class="info-tip" data-tip="Volatility regime. HIGH = large swings expected, LOW = calm market, NORMAL = typical activity.">?</i></span><br><span class="value-sm">{p["vol_regime"]}</span></div>
                <div><span class="mini-label">RSI <i class="info-tip" data-tip="Relative Strength Index (0-100). Above 70 = overbought (may drop), below 30 = oversold (may rise).">?</i></span><br><span class="value-sm">{p["avg_rsi"]:.0f}</span></div>
              </div>
              {actual_str}
            </div>"""

        # Velocity data
        vel = res.get("velocity", {})

        # Volatility data
        vol = res.get("volatility", {})
        vol_label = vol.get("label", "UNKNOWN")
        vol_color = vol.get("color", "muted")
        vol_std = vol.get("std_pct", 0)
        vol_trend = vol.get("vol_trend", "?")
        vol_ann = vol.get("annualized_vol", 0)
        vol_trend_icon = "&#9650;" if vol_trend == "INCREASING" else ("&#9660;" if vol_trend == "DECREASING" else "&#9644;")
        vol_trend_color = "negative" if vol_trend == "INCREASING" else ("positive" if vol_trend == "DECREASING" else "muted")

        # Summary cards
        current_price = windows[-1]["baseline"] + windows[-1]["end_delta"] if windows else 0
        if not bt:
            bt = {"dir_acc": 0, "total": 0, "mae": 0, "hc_acc": 0, "hc_count": 0}
        bt_class = "positive" if bt["dir_acc"] >= 0.53 else ("yellow" if bt["dir_acc"] >= 0.50 else "negative")

        # Window rows
        window_rows = ""
        for w in windows:
            ec = "positive" if w["end_delta"] >= 0 else "negative"
            cross_s = f'{w["last_cross_sec"]:.0f}s→{w["last_cross_direction"][:3]}' if w["last_cross_sec"] else "—"
            rsi_class = "negative" if w["rsi"] > 70 else ("positive" if w["rsi"] < 30 else "")
            window_rows += f"""<tr>
              <td>{w["start"].strftime("%m-%d %H:%M")}</td>
              <td>${w["baseline"]:,.2f}</td>
              <td class="{ec}">${w["end_delta"]:+,.2f}</td>
              <td class="positive">${w["max_positive"]:+,.2f}</td>
              <td class="negative">${w["max_negative"]:+,.2f}</td>
              <td>${w["avg_pos_magnitude"]:+,.2f} / ${w["avg_neg_magnitude"]:+,.2f}</td>
              <td>{cross_s}</td>
              <td class="{rsi_class}">{w["rsi"]:.0f}</td>
              <td>{w["crossings"]}</td>
            </tr>"""

        tab_buttons += f'<button class="tab-btn {active}" onclick="switchTab(\'{ticker}\')" id="btn-{ticker}">{ticker}</button>'

        # Collect chart data
        chart_data_all[ticker] = {
            "24h": res.get("chart_24h", []),
            "7d": res.get("chart_7d", []),
        }

        tab_contents += f"""
        <div class="tab-content {"" if idx > 0 else "active"}" id="tab-{ticker}">
          <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:16px;">
            <h2 style="font-size:1.3em;">{info["name"]} ({ticker}/USDT)</h2>
            <div style="display:flex;align-items:center;gap:16px;">
              <div style="text-align:center;padding:6px 14px;border:1px solid var(--border);border-radius:8px;background:var(--card);">
                <div style="font-size:0.65em;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;">24h Volatility <i class="info-tip" data-tip="How much the price has been swinging over the last 24 hours. HIGH = big moves, LOW = calm market. Arrow shows if volatility is increasing or decreasing.">?</i></div>
                <div style="font-size:1.1em;font-weight:700;" class="{vol_color}">{vol_label}</div>
                <div style="font-size:0.7em;color:var(--muted);">{vol_std:.2f}% &nbsp;<span class="{vol_trend_color}">{vol_trend_icon} {vol_trend}</span></div>
              </div>
              <span id="live-price-{ticker}" style="font-size:1.4em;font-weight:700;">${current_price:,.2f}</span>
            </div>
          </div>

          <div class="pred-section" id="pred-section-{ticker}">
            <h3 style="margin-bottom:12px;font-size:1em;color:var(--blue);">Predictions — Last 3 + Current</h3>
            <div class="pred-grid" id="pred-grid-{ticker}">{pred_cards}</div>
          </div>

          {_render_model_panel(res)}

          <!-- Interactive Price Chart -->
          <div style="margin-top:20px;background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
              <h3 style="font-size:1em;color:var(--blue);">Price Chart</h3>
              <div style="display:flex;gap:4px;">
                <button class="chart-range-btn active" onclick="switchChartRange('{ticker}','24h',this)" style="background:var(--blue);color:#fff;border:1px solid var(--blue);border-radius:4px;padding:4px 10px;cursor:pointer;font-size:0.75em;">24H</button>
                <button class="chart-range-btn" onclick="switchChartRange('{ticker}','7d',this)" style="background:var(--card);color:var(--muted);border:1px solid var(--border);border-radius:4px;padding:4px 10px;cursor:pointer;font-size:0.75em;">7D</button>
              </div>
            </div>
            <div id="chart-{ticker}" style="height:300px;"></div>
          </div>

          <div class="cards" style="margin-top:20px;">
            <div class="card">
              <div class="label">Accuracy in Testing <i class="info-tip" data-tip="How often the model correctly predicted the price direction (up/down) on historical data it hadn't seen during training.">?</i></div>
              <div class="value {bt_class}">{bt["dir_acc"]*100:.1f}%</div>
              <div class="detail">{bt["total"]:,} predictions | MAE: ${bt["mae"]:.2f}</div>
            </div>
            <div class="card">
              <div class="label">High-Conf Test Accuracy <i class="info-tip" data-tip="Accuracy only for predictions where the model was highly confident (>60%). These are the signals most worth acting on.">?</i></div>
              <div class="value {bt_class}">{bt["hc_acc"]*100:.1f}%</div>
              <div class="detail">{bt["hc_count"]:,} high-confidence</div>
            </div>
            <div class="card">
              <div class="label">Avg Last Cross <i class="info-tip" data-tip="Average time (seconds) into a 5-min window when price last crosses the opening price. Lower = choppier, higher = more directional.">?</i></div>
              <div class="value info">{s["avg_last_cross_sec"]:.0f}s</div>
              <div class="detail">Median: {s["median_last_cross_sec"]:.0f}s</div>
            </div>
            <div class="card">
              <div class="label">Avg +/- Magnitude <i class="info-tip" data-tip="Average size of upward and downward moves within each 5-min window. Shows typical positive vs negative pressure.">?</i></div>
              <div class="value"><span class="positive">${s["avg_pos_magnitude"]:+,.2f}</span> <span class="negative">${s["avg_neg_magnitude"]:+,.2f}</span></div>
              <div class="detail">Mean swing from open</div>
            </div>
            <div class="card">
              <div class="label">Avg Peak / Dip <i class="info-tip" data-tip="Average highest and lowest price reached within each window. Shows the typical max upside and downside per 5 minutes.">?</i></div>
              <div class="value"><span class="positive">${s["avg_max_positive"]:+,.2f}</span> <span class="negative">${s["avg_max_negative"]:+,.2f}</span></div>
              <div class="detail">Max extremes per window</div>
            </div>
            <div class="card">
              <div class="label">Ended +/- <i class="info-tip" data-tip="How many 5-min windows ended with a higher price vs lower price. Shows the overall bullish/bearish bias.">?</i></div>
              <div class="value">{s["windows_ended_positive"]:,} / {s["windows_ended_negative"]:,}</div>
              <div class="detail">{s["windows_ended_positive"]/max(s["total_windows"],1)*100:.1f}% positive</div>
            </div>
            <div class="card">
              <div class="label">Avg RSI / Crossings <i class="info-tip" data-tip="RSI: momentum indicator (>70 overbought, <30 oversold). Crossings: how many times price crosses the opening price per window (more = choppier).">?</i></div>
              <div class="value">{s["avg_rsi"]:.0f} / {s["avg_crossings"]:.1f}</div>
              <div class="detail">Intra-window momentum & chop</div>
            </div>
            <div class="card">
              <div class="label">Avg End Delta <i class="info-tip" data-tip="Average price change from open to close of each 5-min window. Positive = overall uptrend, negative = downtrend.">?</i></div>
              <div class="value {"positive" if s["avg_end_delta"]>=0 else "negative"}">${s["avg_end_delta"]:+,.2f}</div>
              <div class="detail">{s["total_windows"]:,} windows over {HISTORY_DAYS}d</div>
            </div>
          </div>

          <div style="margin-top:20px;">
            <h3 style="margin-bottom:12px;font-size:1em;color:var(--green);">Per-Second Velocity (Bot Signals)</h3>
            <div class="cards">
              <div class="card">
                <div class="label">Avg Gain / Loss per Sec <i class="info-tip" data-tip="Average dollar amount gained or lost each second within a window. Shows the speed of price movement up vs down.">?</i></div>
                <div class="value"><span class="positive">${vel.get("avg_gain_per_sec",0):.4f}</span> / <span class="negative">${vel.get("avg_loss_per_sec",0):.4f}</span></div>
                <div class="detail">Ratio: {vel.get("gain_loss_ratio",0):.3f} | {vel.get("pct_seconds_gaining",0):.1f}% gaining</div>
              </div>
              <div class="card">
                <div class="label">Avg Run Duration <i class="info-tip" data-tip="How many consecutive seconds price moves in one direction before reversing. Longer runs = stronger trends within a window.">?</i></div>
                <div class="value"><span class="positive">{vel.get("avg_run_duration_up",0):.1f}s &#9650;</span> <span class="negative">{vel.get("avg_run_duration_down",0):.1f}s &#9660;</span></div>
                <div class="detail">Consecutive seconds in one direction</div>
              </div>
              <div class="card">
                <div class="label">Post-Cross Velocity <i class="info-tip" data-tip="Speed of price movement ($/sec) in the 30 seconds after price crosses zero. Shows how aggressively the market moves after a direction change.">?</i></div>
                <div class="value"><span class="positive">${vel.get("avg_velocity_after_cross_pos",0):.4f}/s</span> <span class="negative">${vel.get("avg_velocity_after_cross_neg",0):.4f}/s</span></div>
                <div class="detail">Avg $/sec in 30s after crossing zero</div>
              </div>
              <div class="card">
                <div class="label">Momentum Decay <i class="info-tip" data-tip="Compares price movement in the 2nd half vs 1st half of each window. Below 1.0 = momentum fades, above 1.0 = momentum accelerates.">?</i></div>
                <div class="value {"positive" if vel.get("momentum_decay_ratio",1)<0.8 else ("negative" if vel.get("momentum_decay_ratio",1)>1.2 else "yellow")}">{vel.get("momentum_decay_ratio",1):.2f}x</div>
                <div class="detail">2nd-half vs 1st-half move ({"fades" if vel.get("momentum_decay_ratio",1)<0.8 else ("accelerates" if vel.get("momentum_decay_ratio",1)>1.2 else "steady")})</div>
              </div>
              <div class="card">
                <div class="label">Avg Time to Peak / Trough <i class="info-tip" data-tip="Average second within the 5-min window when the highest and lowest prices occur. Helps time entries and exits.">?</i></div>
                <div class="value"><span class="positive">{vel.get("avg_time_to_peak_sec",0):.0f}s</span> / <span class="negative">{vel.get("avg_time_to_trough_sec",0):.0f}s</span></div>
                <div class="detail">When max/min typically occurs in window</div>
              </div>
              <div class="card">
                <div class="label">Best Entry Point <i class="info-tip" data-tip="The second into the window where price historically makes its biggest average move. Optimal time to enter a trade.">?</i></div>
                <div class="value info">{vel.get("best_entry_sec",0)}s</div>
                <div class="detail">Sec into window with max avg |move|</div>
              </div>
            </div>
          </div>

          <details style="margin-top:24px;">
            <summary style="cursor:pointer;color:var(--blue);font-size:0.95em;font-weight:600;margin-bottom:12px;">
              Show all {s["total_windows"]:,} windows
            </summary>
            <div class="table-container" style="max-height:60vh;overflow-y:auto;">
              <table>
                <thead><tr>
                  <th>Time (UTC)</th><th>Open</th><th>End Delta</th>
                  <th>Max Up</th><th>Max Down</th><th>Avg +/- Mag</th>
                  <th>Last Cross</th><th>RSI</th><th>Chop</th>
                </tr></thead>
                <tbody>{window_rows}</tbody>
              </table>
            </div>
          </details>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CryptoEdge — Dashboard</title>
<link rel="icon" type="image/png" href="/favicon.png">
<style>
  :root {{ --bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;
           --green:#3fb950;--red:#f85149;--blue:#58a6ff;--yellow:#d29922; }}
  * {{ margin:0;padding:0;box-sizing:border-box; }}
  body {{ background:var(--bg);color:var(--text);font-family:-apple-system,'Segoe UI',sans-serif;padding:16px; }}
  h1 {{ font-size:1.4em;margin-bottom:2px; }}
  .subtitle {{ color:var(--muted);font-size:0.85em;margin-bottom:16px; }}
  .positive {{ color:var(--green); }} .negative {{ color:var(--red); }} .info {{ color:var(--blue); }} .yellow {{ color:var(--yellow); }}
  .cards {{ display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:20px; }}
  .card {{ background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px; }}
  .card .label {{ color:var(--muted);font-size:0.75em;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:4px; }}
  .card .value {{ font-size:1.4em;font-weight:700; }}
  .card .detail {{ color:var(--muted);font-size:0.75em;margin-top:3px; }}
  .info-tip {{ display:inline-block;width:14px;height:14px;line-height:14px;text-align:center;border-radius:50%;
               background:var(--border);color:var(--muted);font-size:0.65em;font-weight:700;cursor:help;
               margin-left:4px;position:relative;vertical-align:middle;font-style:normal;text-transform:none;letter-spacing:0; }}
  .info-tip:hover::after {{ content:attr(data-tip);position:absolute;bottom:120%;left:50%;transform:translateX(-50%);
               background:#1c2333;color:var(--text);padding:8px 12px;border-radius:6px;font-size:11px;line-height:1.4;
               white-space:normal;width:220px;z-index:100;border:1px solid var(--border);
               text-transform:none;letter-spacing:normal;font-weight:400;box-shadow:0 4px 12px rgba(0,0,0,0.4); }}
  .info-tip:hover::before {{ content:'';position:absolute;bottom:110%;left:50%;transform:translateX(-50%);
               border:6px solid transparent;border-top-color:#1c2333;z-index:101; }}

  .tab-bar {{ display:flex;gap:4px;margin-bottom:20px;flex-wrap:wrap; }}
  .tab-btn {{ background:var(--card);color:var(--muted);border:1px solid var(--border);border-radius:6px;
              padding:10px 20px;cursor:pointer;font-size:0.95em;font-weight:600;transition:all 0.2s; }}
  .tab-btn.active {{ background:var(--blue);color:#fff;border-color:var(--blue); }}
  .tab-btn:hover {{ border-color:var(--blue); }}
  .tab-content {{ display:none; }}
  .tab-content.active {{ display:block; }}

  .pred-section {{ background:var(--card);border:1px solid var(--blue);border-radius:10px;padding:16px;margin-bottom:8px; }}
  .pred-grid {{ display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px; }}
  .pred-card {{ background:var(--bg);border:2px solid var(--border);border-radius:8px;padding:14px; }}
  .mini-label {{ color:var(--muted);font-size:0.7em;text-transform:uppercase;letter-spacing:0.04em; }}
  .value-sm {{ font-weight:600;font-size:0.95em; }}

  .table-container {{ overflow-x:auto;border:1px solid var(--border);border-radius:8px; }}
  table {{ width:100%;border-collapse:collapse;font-size:0.8em; }}
  th {{ background:var(--card);color:var(--muted);text-transform:uppercase;font-size:0.7em;
       letter-spacing:0.05em;padding:10px 8px;text-align:left;position:sticky;top:0; }}
  td {{ padding:6px 8px;border-top:1px solid var(--border);white-space:nowrap; }}
  tr:hover td {{ background:rgba(88,166,255,0.05); }}
  details summary {{ list-style:none; }}
  details summary::-webkit-details-marker {{ display:none; }}
  details summary::before {{ content:"+ ";font-weight:700; }}
  details[open] summary::before {{ content:"- "; }}

  /* Mobile responsive */
  @media (max-width: 768px) {{
    body {{ padding:8px; }}
    h1 {{ font-size:1.1em; }}
    .subtitle {{ font-size:0.7em; }}
    .cards {{ grid-template-columns:repeat(2, 1fr);gap:8px; }}
    .card {{ padding:10px; }}
    .card .value {{ font-size:1.1em; }}
    .card .label {{ font-size:0.65em; }}
    .card .detail {{ font-size:0.65em; }}
    .tab-bar {{ gap:3px; }}
    .tab-btn {{ padding:8px 12px;font-size:0.8em; }}
    .pred-grid {{ grid-template-columns:1fr;gap:8px; }}
    .pred-card {{ padding:10px; }}
    .mini-label {{ font-size:0.6em; }}
    .value-sm {{ font-size:0.85em; }}
    table {{ font-size:0.65em; }}
    th {{ padding:6px 4px;font-size:0.6em; }}
    td {{ padding:4px; }}
  }}
  @media (max-width: 480px) {{
    .cards {{ grid-template-columns:repeat(2, 1fr);gap:6px; }}
    .card .value {{ font-size:0.95em; }}
    .tab-btn {{ padding:6px 8px;font-size:0.7em; }}
  }}

  /* Live indicator */
  .live-dot {{ display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);
               animation:pulse 2s ease-in-out infinite; }}
  @keyframes pulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:0.3; }} }}
</style>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
</head>
<body>

<h1>CryptoEdge — 5-Minute Window Analyzer</h1>
<p class="subtitle"><span class="live-dot"></span> <span id="last-update">Live</span> &nbsp;|&nbsp; {HISTORY_DAYS}-day history &nbsp;|&nbsp; 1s resolution &nbsp;|&nbsp; 7-model ensemble</p>
<div style="background:rgba(210,153,34,0.1);border:1px solid rgba(210,153,34,0.3);border-radius:8px;padding:10px 16px;margin-bottom:16px;font-size:0.75em;color:var(--yellow);">
  &#9888; <strong>Not financial advice.</strong> Predictions are probabilistic estimates from ML models trained on historical data. Past accuracy does not guarantee future results. <a href="/disclaimer" style="color:var(--blue);">Full disclaimer</a>
</div>

<div class="tab-bar">{tab_buttons}</div>
{tab_contents}

<script>
const CHART_DATA = {json.dumps(chart_data_all)};
const charts = {{}};
const series = {{}};

function switchTab(ticker) {{
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-'+ticker).classList.add('active');
  document.getElementById('btn-'+ticker).classList.add('active');
  // Resize chart when tab becomes visible
  if (charts[ticker]) {{
    setTimeout(() => charts[ticker].timeScale().fitContent(), 50);
  }} else {{
    initChart(ticker, '24h');
  }}
}}

function initChart(ticker, range) {{
  const container = document.getElementById('chart-' + ticker);
  if (!container) return;
  // Clean up existing chart
  if (charts[ticker]) {{
    charts[ticker].remove();
    delete charts[ticker];
  }}
  container.innerHTML = '';

  const data = CHART_DATA[ticker];
  if (!data) return;
  const points = data[range] || [];
  if (points.length === 0) {{
    container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:0.85em;">No chart data available</div>';
    return;
  }}

  const chart = LightweightCharts.createChart(container, {{
    width: container.clientWidth,
    height: 300,
    layout: {{
      background: {{ type: 'solid', color: '#161b22' }},
      textColor: '#8b949e',
      fontSize: 11,
    }},
    grid: {{
      vertLines: {{ color: '#21262d' }},
      horzLines: {{ color: '#21262d' }},
    }},
    crosshair: {{
      mode: 0,
      vertLine: {{ color: '#58a6ff', width: 1, style: 2 }},
      horzLine: {{ color: '#58a6ff', width: 1, style: 2 }},
    }},
    rightPriceScale: {{
      borderColor: '#30363d',
    }},
    timeScale: {{
      borderColor: '#30363d',
      timeVisible: true,
      secondsVisible: range === '24h',
    }},
  }});

  const areaSeries = chart.addAreaSeries({{
    topColor: 'rgba(88, 166, 255, 0.3)',
    bottomColor: 'rgba(88, 166, 255, 0.02)',
    lineColor: '#58a6ff',
    lineWidth: 2,
    priceFormat: {{ type: 'price', precision: 2, minMove: 0.01 }},
  }});

  const lineData = points.map(p => ({{ time: p.t, value: p.v }}));
  areaSeries.setData(lineData);
  chart.timeScale().fitContent();

  charts[ticker] = chart;
  series[ticker] = areaSeries;

  // Responsive resize
  new ResizeObserver(() => {{
    chart.applyOptions({{ width: container.clientWidth }});
  }}).observe(container);
}}

function switchChartRange(ticker, range, btn) {{
  // Update button styles
  btn.parentElement.querySelectorAll('.chart-range-btn').forEach(b => {{
    b.style.background = 'var(--card)';
    b.style.color = 'var(--muted)';
    b.style.borderColor = 'var(--border)';
    b.classList.remove('active');
  }});
  btn.style.background = 'var(--blue)';
  btn.style.color = '#fff';
  btn.style.borderColor = 'var(--blue)';
  btn.classList.add('active');
  initChart(ticker, range);
}}

// Initialize chart for first visible tab
document.addEventListener('DOMContentLoaded', function() {{
  const tickers = Object.keys(CHART_DATA);
  if (tickers.length > 0) initChart(tickers[0], '24h');
}});
</script>
</body>
</html>"""
    return html


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

def process_asset(ticker, symbol):
    print(f"\n{'='*60}")
    print(f"  {ticker} ({symbol})")
    print(f"{'='*60}")

    raw, start_dt, end_dt = load_or_fetch(symbol, days=HISTORY_DAYS)
    data = parse_klines(raw)
    print(f"  Parsed {len(data):,} data points.")

    windows = analyze_windows(data)
    print(f"  Analyzed {len(windows):,} windows.")

    summary = compute_summary(windows)
    volatility = compute_volatility(windows, lookback_hours=24)
    print(f"  Volatility: {volatility['std_pct']:.3f}% ({volatility['label']}) — trend: {volatility['vol_trend']}")

    print(f"  Computing per-second velocity metrics...")
    velocity = compute_per_second_velocity(data, windows)
    if velocity:
        print(f"    Avg gain/sec: ${velocity['avg_gain_per_sec']:.4f} | Avg loss/sec: ${velocity['avg_loss_per_sec']:.4f}")
        print(f"    Gain/loss ratio: {velocity['gain_loss_ratio']:.3f} | Momentum decay: {velocity['momentum_decay_ratio']:.3f}")
        print(f"    Avg run up: {velocity['avg_run_duration_up']:.1f}s | Avg run down: {velocity['avg_run_duration_down']:.1f}s")
        print(f"    Post-cross velocity: +${velocity['avg_velocity_after_cross_pos']:.4f}/s | -${velocity['avg_velocity_after_cross_neg']:.4f}/s")
        print(f"    Best entry: {velocity['best_entry_sec']}s into window | {velocity['pct_seconds_gaining']:.1f}% of seconds gaining")

    print(f"  Training ensemble...")
    ensemble = EnsemblePredictor()
    ensemble.train_all(windows, verbose=True)

    print(f"  Backtesting...")
    bt = ensemble.backtest(windows)
    print(f"  Dir accuracy: {bt['dir_acc']*100:.1f}% | HC: {bt['hc_acc']*100:.1f}% ({bt['hc_count']})")
    print(f"  MAE: ${bt['mae']:.2f} | Median AE: ${bt['median_ae']:.2f}")

    preds = ensemble.predict_current_and_recent(windows)
    for p in preds:
        tag = "NEXT" if p["is_current"] else "PAST"
        print(f"    [{tag}] {p['window_start'].strftime('%H:%M')} → {p['pred_direction'].upper()} (${p['pred_end_delta']:+,.2f}, conf={p['confidence']*100:.0f}%)")

    # Sample chart data for interactive charts (last 24h at ~30s intervals, plus 7d at 5m intervals)
    chart_data_24h = []
    chart_data_7d = []
    if data:
        now_ms = data[-1][0]
        day_ms = 24 * 3600 * 1000
        week_ms = 7 * day_ms
        # 24h chart: every ~30 seconds
        for ts, price in data:
            if ts >= now_ms - day_ms:
                chart_data_24h.append({"t": ts // 1000, "v": round(price, 2)})
        # Subsample to ~2880 points max (every 30s for 24h)
        if len(chart_data_24h) > 3000:
            step = len(chart_data_24h) // 2880
            chart_data_24h = chart_data_24h[::step]
        # 7d chart: every ~5 minutes
        for ts, price in data:
            if ts >= now_ms - week_ms:
                chart_data_7d.append({"t": ts // 1000, "v": round(price, 2)})
        if len(chart_data_7d) > 2016:
            step = len(chart_data_7d) // 2016
            chart_data_7d = chart_data_7d[::step]

    return {
        "windows": windows,
        "summary": summary,
        "volatility": volatility,
        "velocity": velocity,
        "backtest": bt,
        "predictions": preds,
        "start_dt": start_dt,
        "end_dt": end_dt,
        "chart_24h": chart_data_24h,
        "chart_7d": chart_data_7d,
    }


def main():
    print("=" * 60)
    print("  Multi-Asset 5-Minute Window Analyzer")
    print("=" * 60)

    all_results = {}
    for ticker, info in ASSETS.items():
        all_results[ticker] = process_asset(ticker, info["symbol"])

    output_path = Path(__file__).parent / "crypto_dashboard.html"
    html = generate_dashboard(all_results)
    with open(output_path, "w") as f:
        f.write(html)
    print(f"\nDashboard saved to: {output_path}")
    print("Opening...")

    import subprocess
    subprocess.Popen(["open", str(output_path)])


if __name__ == "__main__":
    main()

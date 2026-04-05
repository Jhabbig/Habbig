"""
smart_betting.py - Advanced bet sizing and signal concordance for the Polymarket stock prediction bot.

Provides:
    - SignalConcordanceFilter: Only bet when multiple independent signal groups agree
    - AdaptiveKelly: Dynamic Kelly fraction based on rolling performance
    - DynamicEdgeThreshold: Require more edge when the model is cold
    - PerformanceTracker: Track which signal combinations work, persist to JSON
    - RiskManager: Portfolio-level risk controls
    - evaluate_bet_enhanced(): Master function tying everything together
"""

import json
import math
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    import numpy as np
except ImportError:
    np = None  # Fallback for environments without numpy


# ---------------------------------------------------------------------------
# Signal category definitions
# ---------------------------------------------------------------------------

SIGNAL_CATEGORIES = {
    "technical": ["rsi", "macd", "bollinger", "z_score", "rsi_signal", "macd_signal",
                  "bollinger_signal", "z_score_signal"],
    "momentum": ["return_5d", "return_10d", "return_20d", "streak", "momentum_5d",
                 "momentum_10d", "momentum_20d", "streak_signal"],
    "volume": ["volume_ratio", "obv", "volume_signal", "obv_signal"],
    "market": ["spy_corr", "sector_momentum", "futures", "spy_correlation",
               "sector_signal", "futures_signal", "market_signal"],
    "sentiment": ["news", "analyst", "pre_market", "news_signal", "analyst_signal",
                  "pre_market_signal", "sentiment_score"],
    "ml": ["ml_prediction", "ml_confidence", "ml_signal", "model_prediction"],
}


def _signal_direction(name, value):
    """Interpret a signal value as UP (+1), DOWN (-1), or NEUTRAL (0)."""
    if value is None:
        return 0

    # Boolean-style signals
    if isinstance(value, bool):
        return 1 if value else -1

    # String-style signals
    if isinstance(value, str):
        low = value.lower()
        if low in ("up", "bullish", "buy", "long", "positive"):
            return 1
        if low in ("down", "bearish", "sell", "short", "negative"):
            return -1
        return 0

    # Numeric signals
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0

    if abs(v) < 1e-9:
        return 0
    return 1 if v > 0 else -1


# ---------------------------------------------------------------------------
# 1. SignalConcordanceFilter
# ---------------------------------------------------------------------------

class SignalConcordanceFilter:
    """Only bet when multiple independent signal groups agree on direction."""

    def __init__(self, min_agreement=0.6):
        self.min_agreement = min_agreement
        self.categories = SIGNAL_CATEGORIES

    def _categorize(self, signals_dict):
        """Map each signal to its category."""
        categorized = {cat: {} for cat in self.categories}
        for sig_name, sig_val in signals_dict.items():
            placed = False
            for cat, keys in self.categories.items():
                if sig_name.lower() in [k.lower() for k in keys]:
                    categorized[cat][sig_name] = sig_val
                    placed = True
                    break
            if not placed:
                # Try substring matching
                for cat, keys in self.categories.items():
                    if any(k.lower() in sig_name.lower() for k in keys):
                        categorized[cat][sig_name] = sig_val
                        placed = True
                        break
        return categorized

    def check_concordance(self, signals_dict, predicted_direction=1):
        """
        Analyze signal groups. Each group votes UP/DOWN based on majority of its signals.

        Args:
            signals_dict: dict of signal_name -> value
            predicted_direction: +1 for UP, -1 for DOWN

        Returns:
            dict with concordance_score, agreeing_groups, dissenting_groups, should_bet
        """
        if not signals_dict:
            return {
                "concordance_score": 0.0,
                "agreeing_groups": [],
                "dissenting_groups": [],
                "should_bet": False,
            }

        categorized = self._categorize(signals_dict)

        agreeing = []
        dissenting = []

        for cat, sigs in categorized.items():
            if not sigs:
                continue
            directions = [_signal_direction(n, v) for n, v in sigs.items()]
            directions = [d for d in directions if d != 0]
            if not directions:
                continue
            group_vote = 1 if sum(directions) >= 0 else -1
            if group_vote == predicted_direction:
                agreeing.append(cat)
            else:
                dissenting.append(cat)

        total_voting = len(agreeing) + len(dissenting)
        if total_voting == 0:
            concordance = 0.0
        else:
            concordance = len(agreeing) / total_voting

        return {
            "concordance_score": round(concordance, 3),
            "agreeing_groups": agreeing,
            "dissenting_groups": dissenting,
            "should_bet": concordance >= self.min_agreement,
        }


# ---------------------------------------------------------------------------
# 2. AdaptiveKelly
# ---------------------------------------------------------------------------

class AdaptiveKelly:
    """Dynamic Kelly fraction that adjusts based on recent win rate."""

    def __init__(self, base_fraction=0.25, lookback_trades=20):
        self.base_fraction = base_fraction
        self.lookback_trades = lookback_trades
        self.min_fraction = 0.05
        self.max_fraction = 0.50

    def get_kelly_fraction(self, recent_trades):
        """
        Calculate adaptive Kelly fraction from recent trade history.

        Args:
            recent_trades: list of dicts with at least a 'won' (bool) key.

        Returns:
            float: adjusted Kelly fraction
        """
        if not recent_trades:
            return self.base_fraction

        trades = recent_trades[-self.lookback_trades:]
        wins = sum(1 for t in trades if t.get("won", False))
        recent_wr = wins / len(trades) if trades else 0.5

        # Compare to a 50% baseline
        baseline = 0.50
        if recent_wr > 0.60:
            # Hot streak: scale up
            fraction = self.base_fraction * (1 + (recent_wr - baseline))
        elif recent_wr < 0.45:
            # Cold streak: scale down
            fraction = self.base_fraction * max(0.2, recent_wr / baseline)
        else:
            fraction = self.base_fraction

        return max(self.min_fraction, min(self.max_fraction, round(fraction, 4)))

    def compute_bet_size(self, confidence, market_prob, balance,
                         recent_trades, max_bet_pct=0.05):
        """
        Compute optimal bet size using adaptive Kelly criterion.

        Args:
            confidence: model's predicted probability (0-1)
            market_prob: current market price / implied probability (0-1)
            balance: current account balance
            recent_trades: list of recent trade dicts
            max_bet_pct: maximum bet as fraction of balance

        Returns:
            float: recommended bet size in dollars
        """
        if balance <= 0 or confidence <= 0 or confidence >= 1:
            return 0.0

        market_prob = max(0.01, min(0.99, market_prob))
        confidence = max(0.01, min(0.99, confidence))

        # Kelly: f* = (bp - q) / b
        # where b = (1/market_prob - 1), p = confidence, q = 1 - confidence
        b = (1.0 / market_prob) - 1.0
        if b <= 0:
            return 0.0

        p = confidence
        q = 1.0 - p
        kelly_full = (b * p - q) / b

        if kelly_full <= 0:
            return 0.0

        fraction = self.get_kelly_fraction(recent_trades)
        kelly_bet = kelly_full * fraction * balance

        max_bet = balance * max_bet_pct
        bet = min(kelly_bet, max_bet)
        bet = max(0.0, round(bet, 2))
        return bet


# ---------------------------------------------------------------------------
# 3. DynamicEdgeThreshold
# ---------------------------------------------------------------------------

class DynamicEdgeThreshold:
    """Require more edge when the model is running cold."""

    def __init__(self, base_threshold=0.08, lookback=20):
        self.base_threshold = base_threshold
        self.lookback = lookback
        self.hot_threshold = 0.05
        self.cold_threshold = 0.15

    def get_threshold(self, recent_trades):
        """
        Return the current edge threshold based on recent accuracy.

        Args:
            recent_trades: list of dicts with 'won' key

        Returns:
            float: edge threshold
        """
        if not recent_trades:
            return self.base_threshold

        trades = recent_trades[-self.lookback:]
        wins = sum(1 for t in trades if t.get("won", False))
        accuracy = wins / len(trades) if trades else 0.5

        if accuracy > 0.60:
            return self.hot_threshold
        elif accuracy < 0.45:
            return self.cold_threshold
        else:
            return self.base_threshold

    def should_bet(self, edge, recent_trades):
        """
        Check if edge exceeds the dynamic threshold.

        Args:
            edge: absolute edge (e.g. |model_prob - market_prob|)
            recent_trades: list of recent trade dicts

        Returns:
            bool
        """
        threshold = self.get_threshold(recent_trades)
        return edge >= threshold


# ---------------------------------------------------------------------------
# 4. PerformanceTracker
# ---------------------------------------------------------------------------

class PerformanceTracker:
    """Track win rates by ticker, signal pattern, and day of week. Persists to JSON."""

    def __init__(self, tracker_path="signal_performance.json"):
        if Path(tracker_path).is_absolute():
            self.path = Path(tracker_path)
        else:
            self.path = Path(__file__).parent / tracker_path

        self.data = {
            "tickers": {},
            "signals": {},
            "day_of_week": {str(i): {"wins": 0, "losses": 0, "pnl": 0.0} for i in range(7)},
            "trades": [],
        }
        self.load()

    def record_trade(self, ticker, signals, direction, won, pnl):
        """
        Record a completed trade.

        Args:
            ticker: str
            signals: dict of signal_name -> value used for this trade
            direction: "up" or "down"
            won: bool
            pnl: float profit/loss
        """
        now = datetime.now()
        dow = str(now.weekday())

        # Ticker stats
        if ticker not in self.data["tickers"]:
            self.data["tickers"][ticker] = {"wins": 0, "losses": 0, "pnl": 0.0, "trades": 0}
        ts = self.data["tickers"][ticker]
        ts["trades"] += 1
        ts["pnl"] = round(ts["pnl"] + pnl, 2)
        if won:
            ts["wins"] += 1
        else:
            ts["losses"] += 1

        # Signal stats
        if signals:
            for sig_name, sig_val in signals.items():
                if sig_name not in self.data["signals"]:
                    self.data["signals"][sig_name] = {"correct": 0, "incorrect": 0, "total": 0}
                ss = self.data["signals"][sig_name]
                ss["total"] += 1
                if won:
                    ss["correct"] += 1
                else:
                    ss["incorrect"] += 1

        # Day of week stats
        ds = self.data["day_of_week"][dow]
        if won:
            ds["wins"] += 1
        else:
            ds["losses"] += 1
        ds["pnl"] = round(ds["pnl"] + pnl, 2)

        # Raw trade log (keep last 500)
        self.data["trades"].append({
            "ticker": ticker,
            "direction": direction,
            "won": won,
            "pnl": round(pnl, 2),
            "timestamp": now.isoformat(),
            "signals": list(signals.keys()) if signals else [],
        })
        if len(self.data["trades"]) > 500:
            self.data["trades"] = self.data["trades"][-500:]

        self.save()

    def get_ticker_stats(self, ticker):
        """Return win rate and P/L for a specific ticker."""
        ts = self.data["tickers"].get(ticker)
        if not ts or ts["trades"] == 0:
            return {"win_rate": None, "pnl": 0.0, "trades": 0}
        return {
            "win_rate": round(ts["wins"] / ts["trades"], 3),
            "pnl": ts["pnl"],
            "trades": ts["trades"],
        }

    def get_signal_stats(self, signal_name):
        """How often a signal's direction matched the outcome."""
        ss = self.data["signals"].get(signal_name)
        if not ss or ss["total"] == 0:
            return {"accuracy": None, "total": 0}
        return {
            "accuracy": round(ss["correct"] / ss["total"], 3),
            "total": ss["total"],
        }

    def get_best_signals(self, n=10):
        """Top N most predictive signals by accuracy (min 5 trades)."""
        results = []
        for sig, stats in self.data["signals"].items():
            if stats["total"] >= 5:
                acc = stats["correct"] / stats["total"]
                results.append((sig, round(acc, 3), stats["total"]))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:n]

    def get_worst_tickers(self, n=3):
        """Worst performing tickers by win rate (min 5 trades)."""
        results = []
        for ticker, stats in self.data["tickers"].items():
            if stats["trades"] >= 5:
                wr = stats["wins"] / stats["trades"]
                results.append((ticker, round(wr, 3), stats["pnl"], stats["trades"]))
        results.sort(key=lambda x: x[1])
        return results[:n]

    def should_skip_ticker(self, ticker, min_trades=10, min_winrate=0.45):
        """Return True if ticker has been consistently unprofitable."""
        ts = self.data["tickers"].get(ticker)
        if not ts or ts["trades"] < min_trades:
            return False
        wr = ts["wins"] / ts["trades"]
        return wr < min_winrate

    def save(self):
        """Persist data to JSON."""
        try:
            with open(self.path, "w") as f:
                json.dump(self.data, f, indent=2, default=str)
        except Exception as e:
            print(f"[PerformanceTracker] Failed to save: {e}")

    def load(self):
        """Load data from JSON if it exists."""
        try:
            if self.path.exists():
                with open(self.path, "r") as f:
                    loaded = json.load(f)
                # Merge with defaults to handle missing keys
                for key in self.data:
                    if key in loaded:
                        self.data[key] = loaded[key]
        except Exception as e:
            print(f"[PerformanceTracker] Failed to load: {e}")


# ---------------------------------------------------------------------------
# 5. RiskManager
# ---------------------------------------------------------------------------

class RiskManager:
    """Portfolio-level risk controls."""

    def __init__(self, max_daily_loss_pct=0.05, max_concurrent_bets=12,
                 max_per_ticker_exposure=0.03, max_directional_exposure=0.7):
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_concurrent_bets = max_concurrent_bets
        self.max_per_ticker_exposure = max_per_ticker_exposure
        self.max_directional_exposure = max_directional_exposure

    def can_place_bet(self, ticker, direction, bet_size, balance,
                      pending_bets, daily_pnl):
        """
        Check all risk limits before placing a bet.

        Args:
            ticker: str
            direction: "up" or "down"
            bet_size: float dollar amount
            balance: float current balance
            pending_bets: list of dicts with keys: ticker, direction, amount
            daily_pnl: float today's realized P/L

        Returns:
            (allowed: bool, reason: str)
        """
        if balance <= 0:
            return False, "Zero or negative balance"

        # Daily loss limit
        max_daily_loss = balance * self.max_daily_loss_pct
        if daily_pnl < 0 and abs(daily_pnl) >= max_daily_loss:
            return False, f"Daily loss limit reached (${abs(daily_pnl):.2f} >= ${max_daily_loss:.2f})"

        # Concurrent bets limit
        if len(pending_bets) >= self.max_concurrent_bets:
            return False, f"Max concurrent bets reached ({self.max_concurrent_bets})"

        # Per-ticker exposure
        ticker_exposure = sum(b.get("amount", 0) for b in pending_bets if b.get("ticker") == ticker)
        max_ticker = balance * self.max_per_ticker_exposure
        if ticker_exposure + bet_size > max_ticker:
            return False, f"Ticker exposure limit for {ticker} (${ticker_exposure + bet_size:.2f} > ${max_ticker:.2f})"

        # Directional exposure
        if pending_bets:
            up_total = sum(b.get("amount", 0) for b in pending_bets if b.get("direction", "").lower() in ("up", "yes"))
            down_total = sum(b.get("amount", 0) for b in pending_bets if b.get("direction", "").lower() in ("down", "no"))
            total_exposure = up_total + down_total + bet_size
            if total_exposure > 0:
                if direction.lower() in ("up", "yes"):
                    up_pct = (up_total + bet_size) / total_exposure
                else:
                    up_pct = up_total / total_exposure
                down_pct = 1.0 - up_pct
                if max(up_pct, down_pct) > self.max_directional_exposure:
                    return False, f"Directional exposure too high ({max(up_pct, down_pct):.0%} > {self.max_directional_exposure:.0%})"

        # Bet size sanity
        if bet_size <= 0:
            return False, "Bet size is zero or negative"
        if bet_size > balance * 0.10:
            return False, f"Single bet too large (${bet_size:.2f} > 10% of balance)"

        return True, "OK"


# ---------------------------------------------------------------------------
# 6. Master evaluation function
# ---------------------------------------------------------------------------

def evaluate_bet_enhanced(ticker, prediction, confidence, market_info, state,
                          signals, recent_trades):
    """
    Master function that uses all components to decide whether and how much to bet.

    Args:
        ticker: str stock ticker
        prediction: "up" or "down"
        confidence: float 0-1, model's confidence
        market_info: dict with at least 'market_prob' and optionally 'token_id', 'condition_id'
        state: dict with 'balance', 'pending_bets' (list), 'daily_pnl' (float)
        signals: dict of signal_name -> value
        recent_trades: list of recent trade dicts with 'won', 'pnl' keys

    Returns:
        dict with:
            - should_bet: bool
            - bet_size: float
            - reason: str (why or why not)
            - concordance: dict from SignalConcordanceFilter
            - edge: float
            - threshold: float
            - kelly_fraction: float
    """
    result = {
        "should_bet": False,
        "bet_size": 0.0,
        "reason": "",
        "concordance": {},
        "edge": 0.0,
        "threshold": 0.0,
        "kelly_fraction": 0.0,
    }

    try:
        balance = state.get("balance", 0)
        pending_bets = state.get("pending_bets", [])
        daily_pnl = state.get("daily_pnl", 0.0)
        market_prob = market_info.get("market_prob", 0.5)

        direction = 1 if prediction.lower() in ("up", "yes") else -1

        # 1. Signal concordance
        concordance_filter = SignalConcordanceFilter(min_agreement=0.6)
        concordance = concordance_filter.check_concordance(signals, direction)
        result["concordance"] = concordance

        if not concordance["should_bet"]:
            result["reason"] = (
                f"Signal concordance too low ({concordance['concordance_score']:.0%}). "
                f"Dissenting: {concordance['dissenting_groups']}"
            )
            return result

        # 2. Edge threshold
        edge = abs(confidence - market_prob)
        result["edge"] = round(edge, 4)

        edge_checker = DynamicEdgeThreshold()
        threshold = edge_checker.get_threshold(recent_trades)
        result["threshold"] = threshold

        if not edge_checker.should_bet(edge, recent_trades):
            result["reason"] = f"Edge too small ({edge:.3f} < threshold {threshold:.3f})"
            return result

        # 3. Performance tracker - skip bad tickers
        tracker = PerformanceTracker()
        if tracker.should_skip_ticker(ticker):
            result["reason"] = f"Ticker {ticker} has poor historical performance"
            return result

        # 4. Kelly bet sizing
        kelly = AdaptiveKelly()
        kelly_fraction = kelly.get_kelly_fraction(recent_trades)
        result["kelly_fraction"] = kelly_fraction
        bet_size = kelly.compute_bet_size(confidence, market_prob, balance, recent_trades)

        if bet_size < 1.0:
            result["reason"] = f"Computed bet size too small (${bet_size:.2f})"
            return result

        # 5. Risk management
        risk_mgr = RiskManager()
        allowed, risk_reason = risk_mgr.can_place_bet(
            ticker, prediction, bet_size, balance, pending_bets, daily_pnl
        )
        if not allowed:
            result["reason"] = f"Risk limit: {risk_reason}"
            return result

        # All checks passed
        result["should_bet"] = True
        result["bet_size"] = bet_size
        result["reason"] = (
            f"Concordance {concordance['concordance_score']:.0%} "
            f"({len(concordance['agreeing_groups'])} groups agree), "
            f"edge {edge:.3f} > {threshold:.3f}, "
            f"Kelly fraction {kelly_fraction:.2f} -> ${bet_size:.2f}"
        )
        return result

    except Exception as e:
        result["reason"] = f"Error in evaluation: {e}"
        return result


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 70)
    print("smart_betting.py - Test Scenarios")
    print("=" * 70)

    # --- Sample signals and trades ---
    sample_signals = {
        "rsi": -0.3,
        "macd": -0.5,
        "bollinger": -0.2,
        "return_5d": -0.04,
        "return_10d": -0.06,
        "streak": -3,
        "volume_ratio": 1.8,
        "obv": -100,
        "spy_corr": 0.7,
        "sector_momentum": -0.03,
        "news": "bearish",
        "ml_prediction": "down",
    }

    sample_trades_good = [{"won": True, "pnl": 5.0}] * 14 + [{"won": False, "pnl": -4.0}] * 6
    sample_trades_bad = [{"won": True, "pnl": 3.0}] * 6 + [{"won": False, "pnl": -5.0}] * 14

    # --- 1. Signal Concordance ---
    print("\n--- Signal Concordance Filter ---")
    scf = SignalConcordanceFilter(min_agreement=0.6)
    result = scf.check_concordance(sample_signals, predicted_direction=-1)
    print(f"  Predicted: DOWN")
    print(f"  Concordance: {result['concordance_score']:.0%}")
    print(f"  Agreeing groups:   {result['agreeing_groups']}")
    print(f"  Dissenting groups: {result['dissenting_groups']}")
    print(f"  Should bet: {result['should_bet']}")

    # --- 2. Adaptive Kelly ---
    print("\n--- Adaptive Kelly ---")
    ak = AdaptiveKelly()
    print(f"  Good run fraction: {ak.get_kelly_fraction(sample_trades_good):.3f}")
    print(f"  Bad run fraction:  {ak.get_kelly_fraction(sample_trades_bad):.3f}")
    print(f"  No history:        {ak.get_kelly_fraction([]):.3f}")

    bet = ak.compute_bet_size(
        confidence=0.65, market_prob=0.50, balance=1000.0,
        recent_trades=sample_trades_good,
    )
    print(f"  Bet size (conf=0.65, mkt=0.50, bal=$1000, good run): ${bet:.2f}")

    bet_cold = ak.compute_bet_size(
        confidence=0.65, market_prob=0.50, balance=1000.0,
        recent_trades=sample_trades_bad,
    )
    print(f"  Bet size (conf=0.65, mkt=0.50, bal=$1000, bad run):  ${bet_cold:.2f}")

    # --- 3. Dynamic Edge Threshold ---
    print("\n--- Dynamic Edge Threshold ---")
    det = DynamicEdgeThreshold()
    print(f"  Threshold (good run): {det.get_threshold(sample_trades_good):.3f}")
    print(f"  Threshold (bad run):  {det.get_threshold(sample_trades_bad):.3f}")
    print(f"  Threshold (no data):  {det.get_threshold([]):.3f}")
    print(f"  Edge 0.10 ok on bad run? {det.should_bet(0.10, sample_trades_bad)}")
    print(f"  Edge 0.10 ok on good run? {det.should_bet(0.10, sample_trades_good)}")

    # --- 4. Performance Tracker ---
    print("\n--- Performance Tracker ---")
    pt = PerformanceTracker(tracker_path="/tmp/test_signal_perf.json")
    pt.record_trade("AAPL", {"rsi": -0.3, "macd": -0.5}, "down", True, 8.50)
    pt.record_trade("AAPL", {"rsi": 0.4, "macd": 0.3}, "up", False, -5.00)
    pt.record_trade("TSLA", {"ml_prediction": "up"}, "up", False, -10.00)
    print(f"  AAPL stats: {pt.get_ticker_stats('AAPL')}")
    print(f"  TSLA stats: {pt.get_ticker_stats('TSLA')}")
    print(f"  RSI signal: {pt.get_signal_stats('rsi')}")
    print(f"  Should skip AAPL (min 10 trades)? {pt.should_skip_ticker('AAPL')}")

    # --- 5. Risk Manager ---
    print("\n--- Risk Manager ---")
    rm = RiskManager()
    pending = [
        {"ticker": "AAPL", "direction": "up", "amount": 15.0},
        {"ticker": "MSFT", "direction": "up", "amount": 15.0},
        {"ticker": "GOOG", "direction": "down", "amount": 10.0},
    ]
    ok, reason = rm.can_place_bet("AMZN", "up", 10.0, 1000.0, pending, -10.0)
    print(f"  Place AMZN UP $10? {ok} ({reason})")

    ok2, reason2 = rm.can_place_bet("AAPL", "up", 25.0, 1000.0, pending, -10.0)
    print(f"  Place AAPL UP $25? {ok2} ({reason2})")

    ok3, reason3 = rm.can_place_bet("NFLX", "down", 10.0, 1000.0, pending, -55.0)
    print(f"  Place NFLX DOWN $10 (daily loss -$55)? {ok3} ({reason3})")

    # --- 6. Full Evaluation ---
    print("\n--- Full evaluate_bet_enhanced ---")
    full_result = evaluate_bet_enhanced(
        ticker="AAPL",
        prediction="down",
        confidence=0.68,
        market_info={"market_prob": 0.55},
        state={"balance": 1000.0, "pending_bets": pending, "daily_pnl": -10.0},
        signals=sample_signals,
        recent_trades=sample_trades_good,
    )
    print(f"  Should bet: {full_result['should_bet']}")
    print(f"  Bet size:   ${full_result['bet_size']:.2f}")
    print(f"  Reason:     {full_result['reason']}")
    print(f"  Edge:       {full_result['edge']}")
    print(f"  Threshold:  {full_result['threshold']}")
    print(f"  Kelly frac: {full_result['kelly_fraction']}")

    # Clean up temp file
    Path("/tmp/test_signal_perf.json").unlink(missing_ok=True)

    print("\n" + "=" * 70)
    print("All tests completed.")
    print("=" * 70)

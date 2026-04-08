#!/usr/bin/env python3
"""
Reactive Trading Bot — Paper Trading

Strategy: REACTIVE, not predictive.
  1. Monitor 5-minute windows for price cross events
  2. When price crosses positive/negative relative to window open:
     - Confirm with velocity, momentum, RSI, and choppiness
     - Enter only if multiple signals align
  3. Exit based on time targets, trailing stops, and take-profit

Safety:
  - Daily loss limit (currently disabled — set MAX_DAILY_LOSS_PCT < 1.0 to enable)
  - Per-trade stop loss (0.4%)
  - Max drawdown circuit breaker (currently disabled — set MAX_DRAWDOWN_PCT < 1.0 to enable)
  - Max position size (15% of balance, hard cap 25%)
  - Max 5 simultaneous positions
  - Per-trade balance check (prevents negative balance)
  - Cooldown after consecutive losses
  - No trading in VOLATILE conditions

Run: python3 trading_bot.py [--live] [--reset]
"""

import json
import os
import tempfile
import time
import requests
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict

# ─── Config ───────────────────────────────────────────────────────────
API_BASE = "http://localhost:8000"
TRADE_LOG = Path(__file__).parent / "trades.json"
BOT_LOG = Path(__file__).parent / "bot_activity.log"
WINDOW_SECONDS = 300

# ─── Safety Parameters ───────────────────────────────────────────────
STARTING_BALANCE = 10000.0

# Position limits
MAX_POSITION_PCT = 0.15       # max 15% of balance per trade
HARD_POSITION_CAP_PCT = 0.25  # absolute max 25% of balance
MAX_OPEN_POSITIONS = 5        # max simultaneous positions
MIN_TRADE_SIZE = 10.0         # minimum $10 per trade

# Loss limits
MAX_DAILY_LOSS_PCT = 1.00     # no daily loss limit (100%)
MAX_DRAWDOWN_PCT = 1.00       # no circuit breaker (100%)
STOP_LOSS_PCT = 0.004         # per-trade stop loss: 0.4% — cut losers fast
TAKE_PROFIT_PCT = 0.008       # per-trade take profit: 0.8% — 2:1 reward/risk
MAX_HOLD_SEC = 600            # hold up to 10 minutes

# Cooldown
COOLDOWN_AFTER_LOSSES = 5     # pause after 5 consecutive losses
COOLDOWN_DURATION_SEC = 120   # cooldown lasts 2 minutes

# ─── Signal Thresholds (Observation-Based Strategy) ───────────────────
MIN_SCORE = 40                # minimum score to enter — be selective
RSI_OVERBOUGHT = 82           # don't buy above this
RSI_OVERSOLD = 18             # don't sell below this
MIN_CROSS_RECENCY_SEC = 180   # cross must be in last 3 min of window
MAX_CROSSINGS = 12            # allow choppier markets


@dataclass
class Position:
    id: str
    ticker: str
    direction: str          # "yes" (long) or "no" (short)
    entry_price: float
    bet_amount: float       # USD staked
    entry_time: str
    target_exit_sec: int
    reason: str
    score: int = 0
    trailing_stop_price: float = 0.0  # trailing stop
    best_price: float = 0.0           # best price seen (for trailing)
    status: str = "open"
    exit_price: float = 0.0
    exit_time: str = ""
    pnl: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""


@dataclass
class BotState:
    balance: float = STARTING_BALANCE
    daily_start_balance: float = STARTING_BALANCE
    daily_date: str = ""
    positions: list = field(default_factory=list)
    closed_trades: list = field(default_factory=list)
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    peak_balance: float = STARTING_BALANCE
    consecutive_losses: int = 0
    cooldown_until: str = ""
    daily_trades: int = 0
    daily_pnl: float = 0.0


def log_activity(msg):
    """Append to activity log file."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}\n"
    with open(BOT_LOG, "a") as f:
        f.write(line)
    print(f"  {msg}")


class TradingBot:
    def __init__(self, live=False):
        self.live = live
        self.state = BotState()
        self.last_signals = {}  # track previous signals to detect changes
        self.load_state()

    def load_state(self):
        if TRADE_LOG.exists():
            try:
                with open(TRADE_LOG) as f:
                    data = json.load(f)
                self.state.balance = data.get("balance", STARTING_BALANCE)
                self.state.total_trades = data.get("total_trades", 0)
                self.state.winning_trades = data.get("winning_trades", 0)
                self.state.losing_trades = data.get("losing_trades", 0)
                self.state.total_pnl = data.get("total_pnl", 0)
                self.state.peak_balance = data.get("peak_balance", STARTING_BALANCE)
                self.state.max_drawdown = data.get("max_drawdown", 0)
                self.state.closed_trades = data.get("closed_trades", [])
                self.state.consecutive_losses = data.get("consecutive_losses", 0)
                self.state.cooldown_until = data.get("cooldown_until", "")
                # Restore open positions
                for pd in data.get("positions", []):
                    self.state.positions.append(Position(**pd))
                log_activity(f"Loaded state: balance=${self.state.balance:,.2f}, "
                           f"{self.state.total_trades} trades, "
                           f"win rate={self.win_rate:.1f}%")
            except Exception as e:
                log_activity(f"Failed to load state: {e}")

    def save_state(self):
        data = {
            "balance": round(self.state.balance, 2),
            "total_trades": self.state.total_trades,
            "winning_trades": self.state.winning_trades,
            "losing_trades": self.state.losing_trades,
            "total_pnl": round(self.state.total_pnl, 2),
            "peak_balance": round(self.state.peak_balance, 2),
            "max_drawdown": round(self.state.max_drawdown, 4),
            "consecutive_losses": self.state.consecutive_losses,
            "cooldown_until": self.state.cooldown_until,
            "positions": [asdict(p) for p in self.state.positions],
            "closed_trades": self.state.closed_trades[-500:],
        }
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(TRADE_LOG), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, TRADE_LOG)
        except BaseException:
            os.unlink(tmp_path)
            raise

    @property
    def win_rate(self):
        if self.state.total_trades == 0:
            return 0.0
        return self.state.winning_trades / self.state.total_trades * 100

    @property
    def avg_win(self):
        wins = [t["pnl"] for t in self.state.closed_trades if t.get("pnl", 0) > 0]
        return sum(wins) / len(wins) if wins else 0

    @property
    def avg_loss(self):
        losses = [t["pnl"] for t in self.state.closed_trades if t.get("pnl", 0) <= 0]
        return sum(losses) / len(losses) if losses else 0

    @property
    def profit_factor(self):
        gross_profit = sum(t["pnl"] for t in self.state.closed_trades if t.get("pnl", 0) > 0)
        gross_loss = abs(sum(t["pnl"] for t in self.state.closed_trades if t.get("pnl", 0) <= 0))
        return gross_profit / gross_loss if gross_loss > 0 else float('inf')

    # ─── API ──────────────────────────────────────────────────────────

    def get_signals(self):
        try:
            resp = requests.get(f"{API_BASE}/_internal/bot/signals", timeout=10)
            if resp.ok:
                return resp.json()
        except Exception as e:
            pass
        return {}

    def get_prices(self):
        try:
            resp = requests.get(f"{API_BASE}/api/prices", timeout=5)
            if resp.ok:
                return resp.json()
        except:
            pass
        return {}

    # ─── Safety Checks ────────────────────────────────────────────────

    def check_daily_reset(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.state.daily_date != today:
            if self.state.daily_date:
                log_activity(f"Day ended: PnL=${self.state.daily_pnl:+,.2f}, "
                           f"trades={self.state.daily_trades}")
            self.state.daily_date = today
            self.state.daily_start_balance = self.state.balance
            self.state.daily_trades = 0
            self.state.daily_pnl = 0.0
            log_activity(f"New trading day: {today} | Balance: ${self.state.balance:,.2f}")

    def is_safe_to_trade(self):
        """Check all safety conditions before allowing new trades."""
        # Daily loss limit
        if self.state.daily_start_balance > 0:
            daily_return = (self.state.balance - self.state.daily_start_balance) / self.state.daily_start_balance
            if daily_return <= -MAX_DAILY_LOSS_PCT:
                return False, "Daily loss limit hit"

        # Circuit breaker: max drawdown
        if self.state.peak_balance > 0:
            dd = (self.state.peak_balance - self.state.balance) / self.state.peak_balance
            if dd >= MAX_DRAWDOWN_PCT:
                return False, f"Circuit breaker: {dd*100:.1f}% drawdown (max {MAX_DRAWDOWN_PCT*100:.0f}%)"

        # Cooldown after consecutive losses
        if self.state.cooldown_until:
            cooldown_end = datetime.fromisoformat(self.state.cooldown_until)
            if datetime.now(timezone.utc) < cooldown_end:
                remaining = (cooldown_end - datetime.now(timezone.utc)).total_seconds()
                return False, f"Cooldown: {remaining:.0f}s remaining after {COOLDOWN_AFTER_LOSSES} consecutive losses"
            else:
                self.state.cooldown_until = ""
                self.state.consecutive_losses = 0

        # Balance too low
        if self.state.balance < MIN_TRADE_SIZE * 2:
            return False, f"Balance too low: ${self.state.balance:.2f}"

        return True, "OK"

    # ─── Signal Evaluation (Pure Observation Strategy) ──────────────────

    def evaluate_signal(self, ticker, signal):
        """
        OBSERVATION-ONLY evaluation. No model predictions used.
        Compares live window behavior against historical averages.
        Returns (direction, score, reason) or (None, 0, reason).
        """
        reasons = []

        # ── Hard blocks ──
        vol_label = signal.get("volatility_label", "UNKNOWN")
        if vol_label == "VOLATILE":
            return None, 0, f"{ticker}: BLOCKED — volatile market"

        rsi = signal.get("rsi", 50)
        crossings = signal.get("crossings", 0)
        if crossings > MAX_CROSSINGS:
            return None, 0, f"{ticker}: BLOCKED — too choppy ({crossings} crossings)"

        # ── Must have a recent cross event ──
        cross_dir = signal.get("last_cross_direction")
        cross_sec = signal.get("last_cross_sec")

        if cross_sec is None or cross_dir is None:
            return None, 0, f"{ticker}: no cross event"

        if cross_sec < (WINDOW_SECONDS - MIN_CROSS_RECENCY_SEC):
            return None, 0, f"{ticker}: cross at {cross_sec:.0f}s — too early in window"

        direction = "yes" if cross_dir == "positive" else "no"

        # ── RSI filter ──
        if direction == "yes" and rsi >= RSI_OVERBOUGHT:
            return None, 0, f"{ticker}: RSI {rsi:.0f} overbought"
        if direction == "no" and rsi <= RSI_OVERSOLD:
            return None, 0, f"{ticker}: RSI {rsi:.0f} oversold"

        # ══════════════════════════════════════════════════════════════
        # OBSERVATION-BASED SCORING (no model predictions)
        # ══════════════════════════════════════════════════════════════
        score = 0

        # 1. CROSS TIMING vs HISTORICAL PEAK/TROUGH (0-20 pts)
        # Does the cross happen near when historically peaks/troughs occur?
        avg_peak_sec = signal.get("avg_time_to_peak", 150)
        avg_trough_sec = signal.get("avg_time_to_trough", 150)
        target_sec = avg_peak_sec if direction == "yes" else avg_trough_sec
        timing_diff = abs(cross_sec - target_sec)

        if timing_diff < 30:
            score += 20; reasons.append(f"Cross@{cross_sec:.0f}s near hist {'peak' if direction=='yes' else 'trough'}@{target_sec:.0f}s")
        elif timing_diff < 60:
            score += 12; reasons.append(f"Cross@{cross_sec:.0f}s close to hist {target_sec:.0f}s")
        elif timing_diff > 120:
            score -= 10; reasons.append(f"Cross@{cross_sec:.0f}s far from hist {target_sec:.0f}s")

        # 2. CURRENT DELTA vs HISTORICAL AVERAGE (0-15 pts)
        # Is this window outperforming what historically positive/negative windows look like?
        current_delta = signal.get("current_delta", 0)
        hist_avg_pos = signal.get("hist_avg_pos_delta", 0)
        hist_avg_neg = signal.get("hist_avg_neg_delta", 0)

        if direction == "yes":
            if hist_avg_pos != 0 and current_delta > 0:
                ratio = current_delta / abs(hist_avg_pos) if hist_avg_pos != 0 else 0
                if ratio >= 0.5:
                    score += 15; reasons.append(f"Delta ${current_delta:.4f} = {ratio:.0%} of hist avg win")
                elif ratio >= 0.2:
                    score += 8; reasons.append(f"Delta building ({ratio:.0%} of hist avg)")
            elif current_delta <= 0:
                score -= 5; reasons.append(f"Delta ${current_delta:.4f} still negative despite cross")
        else:
            if hist_avg_neg != 0 and current_delta < 0:
                ratio = abs(current_delta) / abs(hist_avg_neg) if hist_avg_neg != 0 else 0
                if ratio >= 0.5:
                    score += 15; reasons.append(f"Delta ${current_delta:.4f} = {ratio:.0%} of hist avg loss")
                elif ratio >= 0.2:
                    score += 8; reasons.append(f"Delta building ({ratio:.0%} of hist avg)")
            elif current_delta >= 0:
                score -= 5; reasons.append(f"Delta ${current_delta:.4f} still positive despite cross")

        # 3. POST-CROSS VELOCITY vs HISTORICAL (0-15 pts)
        # Is the velocity after crossing consistent with historical profitable moves?
        if direction == "yes":
            hist_vel = signal.get("avg_velocity_after_cross_pos", 0)
            avg_gain = signal.get("avg_gain_per_sec", 0)
            if hist_vel > 0 and avg_gain > 0:
                score += 15; reasons.append(f"Hist post-cross vel +${hist_vel:.4f}/s, avg gain ${avg_gain:.4f}/s")
            elif hist_vel <= 0:
                score -= 10; reasons.append(f"Hist post-cross vel ${hist_vel:.4f}/s — crosses don't hold")
        else:
            hist_vel = signal.get("avg_velocity_after_cross_neg", 0)
            avg_loss = signal.get("avg_loss_per_sec", 0)
            if hist_vel < 0 and avg_loss > 0:
                score += 15; reasons.append(f"Hist post-cross vel ${hist_vel:.4f}/s, avg loss ${avg_loss:.4f}/s")
            elif hist_vel >= 0:
                score -= 10; reasons.append(f"Hist post-cross vel +${hist_vel:.4f}/s — crosses don't hold")

        # 4. MOMENTUM DECAY (0-10 pts)
        # Does this asset's momentum sustain or fade?
        decay = signal.get("momentum_decay", 1.0)
        if 0.7 <= decay <= 1.3:
            score += 10; reasons.append(f"Momentum holds ({decay:.2f}x)")
        elif decay < 0.5 or decay > 2.0:
            score -= 10; reasons.append(f"Momentum unstable ({decay:.2f}x)")

        # 5. GAIN/LOSS RATIO ALIGNMENT (0-15 pts)
        # Historical: does this asset's gains outweigh losses in our direction?
        gl_ratio = signal.get("gain_loss_ratio", 1.0)
        if direction == "yes":
            if gl_ratio >= 1.05:
                score += 15; reasons.append(f"Hist G/L {gl_ratio:.3f} favors longs")
            elif gl_ratio >= 1.01:
                score += 8; reasons.append(f"Hist G/L {gl_ratio:.3f} slightly favors up")
            elif gl_ratio < 0.95:
                score -= 10; reasons.append(f"Hist G/L {gl_ratio:.3f} — gains weaker than losses")
        else:
            if gl_ratio <= 0.95:
                score += 15; reasons.append(f"Hist G/L {gl_ratio:.3f} favors shorts")
            elif gl_ratio <= 0.99:
                score += 8; reasons.append(f"Hist G/L {gl_ratio:.3f} slightly favors down")
            elif gl_ratio > 1.05:
                score -= 10; reasons.append(f"Hist G/L {gl_ratio:.3f} — losses weaker than gains")

        # 6. RSI ZONE vs HISTORICAL RSI FOR WINNERS (0-10 pts)
        # Is current RSI in the zone where historically the direction we want tends to win?
        hist_rsi_up = signal.get("hist_avg_rsi_when_up", 50)
        hist_rsi_down = signal.get("hist_avg_rsi_when_down", 50)
        if direction == "yes":
            rsi_diff = abs(rsi - hist_rsi_up)
            if rsi_diff < 8:
                score += 10; reasons.append(f"RSI {rsi:.0f} near hist win RSI {hist_rsi_up:.0f}")
            elif rsi_diff < 15:
                score += 5; reasons.append(f"RSI {rsi:.0f} close to hist win zone")
        else:
            rsi_diff = abs(rsi - hist_rsi_down)
            if rsi_diff < 8:
                score += 10; reasons.append(f"RSI {rsi:.0f} near hist loss RSI {hist_rsi_down:.0f}")
            elif rsi_diff < 15:
                score += 5; reasons.append(f"RSI {rsi:.0f} close to hist loss zone")

        # 7. CHOPPINESS vs HISTORICAL WINNERS (0-10 pts)
        # Are crossings similar to historically winning windows?
        hist_cross_win = signal.get("hist_avg_crossings_winners", 3)
        cross_diff = abs(crossings - hist_cross_win)
        if cross_diff <= 1:
            score += 10; reasons.append(f"{crossings} crossings (hist winners avg {hist_cross_win:.1f})")
        elif cross_diff <= 2:
            score += 5; reasons.append(f"{crossings} crossings (hist winners ~{hist_cross_win:.1f})")

        # 8. CURRENT WINDOW STRENGTH (0-10 pts)
        # How does current max excursion compare to historical average?
        curr_max_up = signal.get("current_max_up", 0)
        curr_max_down = signal.get("current_max_down", 0)
        hist_max_up = signal.get("hist_avg_max_up", 0)
        hist_max_down = signal.get("hist_avg_max_down", 0)

        if direction == "yes" and hist_max_up > 0:
            up_ratio = curr_max_up / hist_max_up
            if up_ratio >= 0.8:
                score += 10; reasons.append(f"Max up ${curr_max_up:.4f} = {up_ratio:.0%} of hist avg")
            elif up_ratio >= 0.4:
                score += 5; reasons.append(f"Max up building ({up_ratio:.0%} of hist)")
        elif direction == "no" and hist_max_down < 0:
            down_ratio = curr_max_down / hist_max_down
            if down_ratio >= 0.8:
                score += 10; reasons.append(f"Max down ${curr_max_down:.4f} = {down_ratio:.0%} of hist avg")
            elif down_ratio >= 0.4:
                score += 5; reasons.append(f"Max down building ({down_ratio:.0%} of hist)")

        # 9. PERCENT SECONDS GAINING (0-5 pts)
        pct_gaining = signal.get("pct_seconds_gaining", 50)
        if direction == "yes" and pct_gaining > 52:
            score += 5; reasons.append(f"{pct_gaining:.1f}% of seconds gaining")
        elif direction == "no" and pct_gaining < 48:
            score += 5; reasons.append(f"Only {pct_gaining:.1f}% of seconds gaining")

        # 10. MODEL BONUS — small edge, never decisive (0-5 pts)
        # Can't make or break a trade — just "better shoes"
        pred_dir = signal.get("pred_direction", "")
        pred_conf = signal.get("pred_confidence", 0)
        if pred_dir == cross_dir and pred_conf >= 0.6:
            score += 5; reasons.append(f"Model nod ({pred_conf:.0%})")

        # ── Decision ──
        if score >= MIN_SCORE:
            return direction, score, f"{'; '.join(reasons)}"
        else:
            return None, score, f"{'; '.join(reasons)}"

    # ─── Position Management ─────────────────────────────────────────

    def size_position(self, score):
        """Fixed $100 per trade."""
        return 100.0

    def open_position(self, ticker, direction, score, reason, signal):
        """Open a paper trade with trailing stop."""
        price = signal.get("price", 0)
        if price <= 0:
            return

        amount = self.size_position(score)
        if amount < MIN_TRADE_SIZE:
            return
        if self.state.balance < amount:
            return

        # Set trailing stop based on direction
        if direction == "yes":
            trailing_stop = price * (1 - STOP_LOSS_PCT)
        else:
            trailing_stop = price * (1 + STOP_LOSS_PCT)

        pos = Position(
            id=f"{ticker}-{int(time.time())}",
            ticker=ticker,
            direction=direction,
            entry_price=price,
            bet_amount=amount,
            entry_time=datetime.now(timezone.utc).isoformat(),
            target_exit_sec=WINDOW_SECONDS,  # hold for one window
            reason=reason,
            score=score,
            trailing_stop_price=trailing_stop,
            best_price=price,
        )
        self.state.positions.append(pos)
        self.state.balance -= amount

        log_activity(
            f"OPEN {ticker} {direction.upper()} | "
            f"${amount:.2f} @ ${price:,.4f} | "
            f"score={score} | stop=${trailing_stop:,.4f} | "
            f"reason: {reason}"
        )

    def check_exits(self, prices):
        """Check open positions for exit conditions with trailing stops."""
        to_close = []
        now = datetime.now(timezone.utc)

        for pos in self.state.positions:
            if pos.status != "open":
                continue

            current_price = prices.get(pos.ticker, 0)
            if current_price <= 0:
                continue

            entry_time = datetime.fromisoformat(pos.entry_time)
            elapsed = (now - entry_time).total_seconds()

            # Calculate unrealized P&L
            price_change_pct = (current_price - pos.entry_price) / pos.entry_price
            if pos.direction == "yes":
                pnl_pct = price_change_pct
            else:
                pnl_pct = -price_change_pct
            pnl = pos.bet_amount * pnl_pct

            # Update trailing stop — tighten as time passes
            trail_pct = STOP_LOSS_PCT
            if elapsed > 300:
                trail_pct = STOP_LOSS_PCT * 0.5  # tighten stop after 5 min

            if pos.direction == "yes":
                if current_price > pos.best_price:
                    pos.best_price = current_price
                new_stop = pos.best_price * (1 - trail_pct)
                if new_stop > pos.trailing_stop_price:
                    pos.trailing_stop_price = new_stop
            else:
                if current_price < pos.best_price:
                    pos.best_price = current_price
                new_stop = pos.best_price * (1 + trail_pct)
                if new_stop < pos.trailing_stop_price:
                    pos.trailing_stop_price = new_stop

            # ── Exit conditions (priority order) ──
            exit_reason = None

            # 1. Trailing stop hit
            if pos.direction == "yes" and current_price <= pos.trailing_stop_price:
                exit_reason = f"trailing stop @ ${pos.trailing_stop_price:,.4f}"
            elif pos.direction == "no" and current_price >= pos.trailing_stop_price:
                exit_reason = f"trailing stop @ ${pos.trailing_stop_price:,.4f}"

            # 2. Take profit
            if pnl_pct >= TAKE_PROFIT_PCT:
                exit_reason = f"take profit ({pnl_pct*100:+.2f}%)"

            # 3. Max hold time — exit at market, win or lose
            if elapsed >= MAX_HOLD_SEC:
                exit_reason = f"max hold {MAX_HOLD_SEC}s exceeded ({pnl_pct*100:+.2f}%)"

            # 4. Break-even stop after 4 min if in profit
            if elapsed >= 240 and pnl_pct > 0.001:
                # Move stop to break-even + tiny buffer
                be_stop = pos.entry_price * 1.001 if pos.direction == "yes" else pos.entry_price * 0.999
                if pos.direction == "yes" and be_stop > pos.trailing_stop_price:
                    pos.trailing_stop_price = be_stop
                elif pos.direction == "no" and be_stop < pos.trailing_stop_price:
                    pos.trailing_stop_price = be_stop

            if exit_reason:
                pos.status = "closed"
                pos.exit_price = current_price
                pos.exit_time = now.isoformat()
                pos.pnl = round(pnl, 2)
                pos.pnl_pct = round(pnl_pct * 100, 3)
                pos.exit_reason = exit_reason
                to_close.append(pos)

        for pos in to_close:
            self.state.positions.remove(pos)
            self.state.balance += pos.bet_amount + pos.pnl
            self.state.total_trades += 1
            self.state.total_pnl += pos.pnl
            self.state.daily_trades += 1
            self.state.daily_pnl += pos.pnl

            if pos.pnl > 0:
                self.state.winning_trades += 1
                self.state.consecutive_losses = 0
            else:
                self.state.losing_trades += 1
                self.state.consecutive_losses += 1

                # Trigger cooldown after consecutive losses
                if self.state.consecutive_losses >= COOLDOWN_AFTER_LOSSES:
                    cooldown_end = now + timedelta(seconds=COOLDOWN_DURATION_SEC)
                    self.state.cooldown_until = cooldown_end.isoformat()
                    log_activity(f"COOLDOWN triggered: {COOLDOWN_AFTER_LOSSES} consecutive losses. "
                               f"Pausing until {cooldown_end.strftime('%H:%M:%S')}")

            # Track drawdown
            if self.state.balance > self.state.peak_balance:
                self.state.peak_balance = self.state.balance
            dd = (self.state.peak_balance - self.state.balance) / self.state.peak_balance
            if dd > self.state.max_drawdown:
                self.state.max_drawdown = dd

            self.state.closed_trades.append(asdict(pos))

            icon = "WIN" if pos.pnl > 0 else "LOSS"
            log_activity(
                f"CLOSE [{icon}] {pos.ticker} {pos.direction.upper()} | "
                f"PnL: ${pos.pnl:+,.2f} ({pos.pnl_pct:+.2f}%) | "
                f"${pos.entry_price:,.4f} -> ${pos.exit_price:,.4f} | "
                f"{pos.exit_reason}"
            )

        if to_close:
            self.save_state()

    # ─── Display ──────────────────────────────────────────────────────

    def print_status(self):
        open_positions = [p for p in self.state.positions if p.status == "open"]
        dd = (self.state.peak_balance - self.state.balance) / self.state.peak_balance * 100 if self.state.peak_balance > 0 else 0

        print(f"\n  {'='*60}")
        print(f"  PAPER TRADING STATUS")
        print(f"  {'='*60}")
        print(f"  Balance:     ${self.state.balance:>10,.2f}  (started: ${STARTING_BALANCE:,.2f})")
        print(f"  Total PnL:   ${self.state.total_pnl:>10,.2f}  ({self.state.total_pnl/STARTING_BALANCE*100:+.2f}%)")
        print(f"  Daily PnL:   ${self.state.daily_pnl:>10,.2f}")
        print(f"  {'─'*60}")
        print(f"  Trades: {self.state.total_trades:>4}  |  Win: {self.state.winning_trades}  |  Loss: {self.state.losing_trades}")
        print(f"  Win Rate:  {self.win_rate:>5.1f}%  |  Profit Factor: {self.profit_factor:.2f}")
        print(f"  Avg Win:   ${self.avg_win:>8,.2f}  |  Avg Loss: ${self.avg_loss:>8,.2f}")
        print(f"  Max DD:    {dd:>5.2f}%  |  Peak: ${self.state.peak_balance:,.2f}")
        print(f"  Consec Losses: {self.state.consecutive_losses}")

        if open_positions:
            print(f"  {'─'*60}")
            print(f"  OPEN POSITIONS ({len(open_positions)}):")
            for p in open_positions:
                print(f"    {p.ticker} {p.direction.upper()} ${p.bet_amount:.2f} @ ${p.entry_price:,.4f} "
                      f"(stop: ${p.trailing_stop_price:,.4f})")
        print(f"  {'='*60}")

    def print_scan_summary(self, signals, evaluations):
        """Print what the bot sees each cycle."""
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        active = len([p for p in self.state.positions if p.status == "open"])
        print(f"\n  [{timestamp}] Scanning {len(signals)} assets | "
              f"Open: {active}/{MAX_OPEN_POSITIONS} | "
              f"Balance: ${self.state.balance:,.2f}")

        for ticker, (direction, score, reason) in evaluations.items():
            if direction:
                print(f"    >> {ticker}: {direction.upper()} score={score} — {reason}")
            else:
                # Only show blocked/skipped if verbose
                pass

    # ─── Main Loop ────────────────────────────────────────────────────

    def run(self):
        mode = "LIVE" if self.live else "PAPER"
        print(f"\n{'='*60}")
        print(f"  Reactive Trading Bot — {mode} MODE")
        print(f"{'='*60}")
        print(f"  Balance:        ${self.state.balance:,.2f}")
        print(f"  Max position:   {MAX_POSITION_PCT*100:.0f}% (${self.state.balance * MAX_POSITION_PCT:,.2f})")
        print(f"  Daily loss cap: {MAX_DAILY_LOSS_PCT*100:.0f}%")
        print(f"  Stop loss:      {STOP_LOSS_PCT*100:.1f}%")
        print(f"  Take profit:    {TAKE_PROFIT_PCT*100:.1f}%")
        print(f"  Max drawdown:   {MAX_DRAWDOWN_PCT*100:.0f}%")
        print(f"  Cooldown:       {COOLDOWN_DURATION_SEC}s after {COOLDOWN_AFTER_LOSSES} losses")
        print(f"{'='*60}")

        if self.live:
            print("\n  !! LIVE MODE — Real money at risk !!")
            print("  Press Ctrl+C to stop.\n")

        log_activity(f"Bot started in {mode} mode. Balance: ${self.state.balance:,.2f}")

        cycle = 0
        while True:
            try:
                cycle += 1
                self.check_daily_reset()

                # Safety check
                safe, reason = self.is_safe_to_trade()

                # Always get prices and check exits, even during cooldown
                prices = self.get_prices()
                if prices:
                    self.check_exits(prices)

                if not safe:
                    if cycle % 60 == 1:  # log every 5 min during lockout
                        log_activity(f"Trading paused: {reason}")
                    time.sleep(5)
                    continue

                # Get signals
                signals = self.get_signals()
                if not signals:
                    if cycle % 12 == 1:
                        print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Waiting for API...")
                    time.sleep(5)
                    continue

                # Evaluate all assets
                evaluations = {}
                for ticker, signal in signals.items():
                    direction, score, reason = self.evaluate_signal(ticker, signal)
                    evaluations[ticker] = (direction, score, reason)

                # Show scan results every 6 cycles (30s)
                if cycle % 6 == 1:
                    self.print_scan_summary(signals, evaluations)

                # Open new positions
                open_count = len([p for p in self.state.positions if p.status == "open"])
                open_tickers = {p.ticker for p in self.state.positions if p.status == "open"}

                # Sort by score descending — take best signals first
                ranked = sorted(
                    [(t, d, s, r) for t, (d, s, r) in evaluations.items() if d is not None],
                    key=lambda x: x[2], reverse=True
                )

                for ticker, direction, score, reason in ranked:
                    if open_count >= MAX_OPEN_POSITIONS:
                        break
                    if ticker in open_tickers:
                        continue

                    self.open_position(ticker, direction, score, reason, signals[ticker])
                    open_count += 1
                    self.save_state()

                # Full status every 120 cycles (10 min)
                if cycle % 120 == 0:
                    self.print_status()

                time.sleep(5)

            except KeyboardInterrupt:
                print("\n\n  Bot stopped by user.")
                log_activity("Bot stopped by user")
                self.save_state()
                self.print_status()
                break
            except Exception as e:
                log_activity(f"Error: {e}")
                time.sleep(10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reactive Trading Bot")
    parser.add_argument("--live", action="store_true", help="Enable live trading (default: paper)")
    parser.add_argument("--reset", action="store_true", help="Reset balance and trade history")
    args = parser.parse_args()

    if args.reset:
        if TRADE_LOG.exists():
            TRADE_LOG.unlink()
        print("  Trade history reset.")

    bot = TradingBot(live=args.live)
    bot.run()

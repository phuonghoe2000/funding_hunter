"""
Backtest - Momentum Strategy Backtest

Chạy backtest strategy momentum trên dữ liệu 15m candles có sẵn.
Dùng cùng logic signal detection, volume confirm, RSI filter, trailing TP/SL
từ momentum_strategy.py

Usage:
    python backtest.py                       # Mặc định POWER/USDT
    python backtest.py flowusdt_15m_1000.json
    python backtest.py pippinusdt_15m_1000.json
"""

import json
import sys
import os
import io

# Fix Windows console encoding (only when run directly, not when imported)
if __name__ == '__main__' and sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Dict
from collections import deque
from enum import Enum


# ==================== Data Classes ====================

class TradeDirection(Enum):
    LONG = "long"
    SHORT = "short"


@dataclass
class BacktestConfig:
    """Config - matches autotrade_config.json"""
    price_change_threshold: float = 0.5     # % change to trigger
    lookback_candles: int = 10              # candles to look back
    position_volume_usdt: float = 15.0      # USDT per trade
    leverage: int = 5
    take_profit_pct: float = 3.5            # TP %
    stop_loss_pct: float = 0.3              # SL %
    use_trailing_tp: bool = True
    tp_extension_pct: float = 0.3           # Trailing TP extend %
    max_tp_extensions: int = 5              # Max TP extends
    use_volume_confirmation: bool = True
    volume_multiplier: float = 2.0          # Volume must be Nx average
    volume_lookback: int = 20              # Candles for volume avg
    use_rsi_filter: bool = True
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    cooldown_candles: int = 4               # Cooldown sau khi đóng lệnh (= 60s / 15m ≈ 1, dùng 4 cho an toàn)
    max_momentum_pct: float = 0.0           # Skip if |momentum| > this (0 = disabled)
    min_atr_pct: float = 0.0               # Skip if ATR% < this (0 = disabled)
    max_atr_pct: float = 0.0               # Skip if ATR% > this (0 = disabled)
    blocked_hours: list = None             # Block specific UTC hours e.g. [4,8,21,22]
    use_ema_trend: bool = False             # Skip trades against EMA trend
    ema_period: int = 50                    # EMA period for trend detection
    ema_slope_candles: int = 3             # Compare EMA now vs N candles ago


@dataclass
class BacktestTrade:
    """Một trade trong backtest"""
    direction: TradeDirection
    entry_price: float
    entry_time: datetime
    size: float
    tp_price: float
    sl_price: float
    close_price: float = 0.0
    close_time: Optional[datetime] = None
    close_reason: str = ""
    pnl_pct: float = 0.0
    pnl_usdt: float = 0.0
    tp_hit_count: int = 0
    highest_price: float = 0.0
    lowest_price: float = float('inf')


# ==================== RSI Calculation ====================

def calculate_rsi(close_prices: list, period: int = 14) -> float:
    """RSI - Wilder's smoothing (giống momentum_strategy.py)"""
    if len(close_prices) < period + 1:
        return 50.0

    changes = [close_prices[i] - close_prices[i - 1] for i in range(1, len(close_prices))]

    gains = [max(c, 0) for c in changes[:period]]
    losses = [abs(min(c, 0)) for c in changes[:period]]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for c in changes[period:]:
        gain = max(c, 0)
        loss = abs(min(c, 0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_ema(close_prices: list, period: int) -> float:
    """EMA - Exponential Moving Average"""
    if len(close_prices) < period:
        return close_prices[-1] if close_prices else 0.0
    k = 2.0 / (period + 1)
    ema = sum(close_prices[:period]) / period
    for price in close_prices[period:]:
        ema = price * k + ema * (1 - k)
    return ema


# ==================== Backtest Engine ====================

class BacktestEngine:
    def __init__(self, config: BacktestConfig, silent: bool = False):
        self.config = config
        self.silent = silent
        self.trades: List[BacktestTrade] = []
        self.active_trade: Optional[BacktestTrade] = None
        self.cooldown_until: int = 0  # candle index until cooldown expires

    def run(self, klines: list) -> List[BacktestTrade]:
        """Run backtest trên klines data"""
        n = len(klines)
        if not self.silent:
            print(f"  Tổng candles: {n}")
            print(f"  Thời gian: {self._ts(klines[0][0])} → {self._ts(klines[-1][6])}")
            print()

        close_prices = [float(k[4]) for k in klines]
        volumes = [float(k[5]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]

        for i in range(self.config.lookback_candles + self.config.volume_lookback, n):
            candle = klines[i]
            current_close = close_prices[i]
            current_high = float(candle[2])
            current_low = float(candle[3])
            current_volume = volumes[i]
            candle_time = self._ts(candle[0])

            # Monitor active trade (check TP/SL on this candle's high/low)
            if self.active_trade:
                self._monitor_trade(self.active_trade, current_high, current_low, current_close, candle_time, i)
                if self.active_trade and self.active_trade.close_reason:
                    self.trades.append(self.active_trade)
                    self.cooldown_until = i + self.config.cooldown_candles
                    self.active_trade = None
                continue  # Don't open new trade while one is active

            # Cooldown check
            if i < self.cooldown_until:
                continue

            # Check signal
            signal = self._check_signal(close_prices, volumes, highs, lows, i, candle_time)
            if signal:
                self._open_trade(signal, current_close, candle_time)

        # Close any remaining trade at last price
        if self.active_trade:
            last_close = close_prices[-1]
            last_time = self._ts(klines[-1][6])
            self._close_trade(self.active_trade, last_close, last_time, "END_OF_DATA")
            self.trades.append(self.active_trade)
            self.active_trade = None

        return self.trades

    def _check_signal(self, closes: list, volumes: list, highs: list, lows: list, idx: int, candle_time: datetime = None) -> Optional[TradeDirection]:
        """Check momentum signal at candle index"""
        lookback = self.config.lookback_candles
        current = closes[idx]
        old = closes[idx - lookback]

        if old == 0:
            return None

        price_change_pct = ((current - old) / old) * 100

        # Direction
        direction = None
        threshold = self.config.price_change_threshold
        if price_change_pct >= threshold:
            direction = TradeDirection.LONG
        elif price_change_pct <= -threshold:
            direction = TradeDirection.SHORT
        else:
            return None

        # Max momentum filter (skip mean-reversion extremes)
        if self.config.max_momentum_pct > 0 and abs(price_change_pct) > self.config.max_momentum_pct:
            return None

        # Blocked hours filter (UTC)
        if self.config.blocked_hours and candle_time is not None:
            from datetime import timezone
            hour = candle_time.astimezone(timezone.utc).hour
            if hour in self.config.blocked_hours:
                return None

        # Volume confirmation
        if self.config.use_volume_confirmation:
            current_vol = volumes[idx]
            vol_start = max(0, idx - self.config.volume_lookback)
            avg_vol = sum(volumes[vol_start:idx]) / max(1, idx - vol_start)
            if avg_vol > 0:
                vol_ratio = current_vol / avg_vol
                if vol_ratio < self.config.volume_multiplier:
                    return None

        # RSI filter
        if self.config.use_rsi_filter:
            rsi_data = closes[max(0, idx - self.config.rsi_period - 10):idx + 1]
            rsi = calculate_rsi(rsi_data, self.config.rsi_period)
            if direction == TradeDirection.LONG and rsi > self.config.rsi_overbought:
                return None
            if direction == TradeDirection.SHORT and rsi < self.config.rsi_oversold:
                return None

        # Min/Max ATR filter
        if self.config.min_atr_pct > 0 or self.config.max_atr_pct > 0:
            atr_ranges = [highs[j] - lows[j] for j in range(max(0, idx - 14), idx)]
            atr = sum(atr_ranges) / len(atr_ranges) if atr_ranges else 0
            atr_pct = atr / current * 100 if current > 0 else 0
            if self.config.min_atr_pct > 0 and atr_pct < self.config.min_atr_pct:
                return None
            if self.config.max_atr_pct > 0 and atr_pct > self.config.max_atr_pct:
                return None

        # EMA trend filter (skip trades against trend)
        if self.config.use_ema_trend:
            p = self.config.ema_period
            sc = self.config.ema_slope_candles
            if idx >= p + sc:
                ema_now  = calculate_ema(closes[idx - p - sc: idx + 1], p)
                ema_prev = calculate_ema(closes[idx - p - sc: idx - sc + 1], p)
                trending_up   = ema_now > ema_prev
                trending_down = ema_now < ema_prev
                if direction == TradeDirection.SHORT and trending_up:
                    return None  # EMA dốc lên -> cấm SHORT
                if direction == TradeDirection.LONG and trending_down:
                    return None  # EMA dốc xuống -> cấm LONG

        return direction

    def _open_trade(self, direction: TradeDirection, price: float, time: datetime):
        """Open a new trade"""
        size = self.config.position_volume_usdt / price
        if size <= 0:
            return

        if direction == TradeDirection.LONG:
            tp = price * (1 + self.config.take_profit_pct / 100)
            sl = price * (1 - self.config.stop_loss_pct / 100)
        else:
            tp = price * (1 - self.config.take_profit_pct / 100)
            sl = price * (1 + self.config.stop_loss_pct / 100)

        self.active_trade = BacktestTrade(
            direction=direction,
            entry_price=price,
            entry_time=time,
            size=size,
            tp_price=tp,
            sl_price=sl,
            highest_price=price,
            lowest_price=price
        )

    def _monitor_trade(self, trade: BacktestTrade, high: float, low: float, close: float, time: datetime, idx: int):
        """Monitor trade with candle's high/low for TP/SL"""

        # Update extremes
        trade.highest_price = max(trade.highest_price, high)
        trade.lowest_price = min(trade.lowest_price, low)

        if trade.direction == TradeDirection.LONG:
            # Check SL first (worst case - hit low)
            if low <= trade.sl_price:
                reason = "STOP_LOSS" if trade.tp_hit_count == 0 else f"TRAILING_SL(TP×{trade.tp_hit_count})"
                self._close_trade(trade, trade.sl_price, time, reason)
                return

            # Check TP (hit high)
            if high >= trade.tp_price:
                if self.config.use_trailing_tp and (
                    self.config.max_tp_extensions == 0 or trade.tp_hit_count < self.config.max_tp_extensions
                ):
                    # Trailing: move SL to old TP, extend TP
                    trade.sl_price = trade.tp_price
                    trade.tp_price = high * (1 + self.config.tp_extension_pct / 100)
                    trade.tp_hit_count += 1
                else:
                    self._close_trade(trade, trade.tp_price, time, f"TAKE_PROFIT(TP×{trade.tp_hit_count})")
                    return

        else:  # SHORT
            # Check SL first (worst case - hit high)
            if high >= trade.sl_price:
                reason = "STOP_LOSS" if trade.tp_hit_count == 0 else f"TRAILING_SL(TP×{trade.tp_hit_count})"
                self._close_trade(trade, trade.sl_price, time, reason)
                return

            # Check TP (hit low)
            if low <= trade.tp_price:
                if self.config.use_trailing_tp and (
                    self.config.max_tp_extensions == 0 or trade.tp_hit_count < self.config.max_tp_extensions
                ):
                    trade.sl_price = trade.tp_price
                    trade.tp_price = low * (1 - self.config.tp_extension_pct / 100)
                    trade.tp_hit_count += 1
                else:
                    self._close_trade(trade, trade.tp_price, time, f"TAKE_PROFIT(TP×{trade.tp_hit_count})")
                    return

    def _close_trade(self, trade: BacktestTrade, price: float, time: datetime, reason: str):
        trade.close_price = price
        trade.close_time = time
        trade.close_reason = reason

        if trade.direction == TradeDirection.LONG:
            trade.pnl_pct = ((price - trade.entry_price) / trade.entry_price) * 100
        else:
            trade.pnl_pct = ((trade.entry_price - price) / trade.entry_price) * 100

        trade.pnl_usdt = trade.pnl_pct / 100 * self.config.position_volume_usdt * self.config.leverage

    @staticmethod
    def _ts(ms) -> datetime:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


# ==================== Report ====================

def print_report(trades: List[BacktestTrade], config: BacktestConfig):
    """Print backtest report"""
    if not trades:
        print("\n❌ Không có trade nào!")
        return

    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]
    total_pnl = sum(t.pnl_usdt for t in trades)
    total_pnl_pct = sum(t.pnl_pct for t in trades)

    long_trades = [t for t in trades if t.direction == TradeDirection.LONG]
    short_trades = [t for t in trades if t.direction == TradeDirection.SHORT]

    sl_trades = [t for t in trades if "STOP_LOSS" in t.close_reason]
    tp_trades = [t for t in trades if "TAKE_PROFIT" in t.close_reason or "TRAILING" in t.close_reason]

    max_win = max((t.pnl_usdt for t in trades), default=0)
    max_loss = min((t.pnl_usdt for t in trades), default=0)
    avg_win = sum(t.pnl_usdt for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.pnl_usdt for t in losses) / len(losses) if losses else 0

    # Drawdown
    equity = 0
    peak = 0
    max_dd = 0
    for t in trades:
        equity += t.pnl_usdt
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)

    # Consecutive losses
    max_consec_loss = 0
    current_consec = 0
    for t in trades:
        if t.pnl_pct <= 0:
            current_consec += 1
            max_consec_loss = max(max_consec_loss, current_consec)
        else:
            current_consec = 0

    print("\n" + "=" * 70)
    print("                    📊 BACKTEST REPORT")
    print("=" * 70)

    print(f"\n  Config:")
    print(f"    Threshold: {config.price_change_threshold}% / {config.lookback_candles} candles")
    print(f"    Volume:    {config.position_volume_usdt} USDT × {config.leverage}x leverage")
    print(f"    TP/SL:     {config.take_profit_pct}% / {config.stop_loss_pct}%")
    print(f"    Trailing:  {'ON' if config.use_trailing_tp else 'OFF'} (+{config.tp_extension_pct}%, max {config.max_tp_extensions})")
    print(f"    Filters:   Vol {'ON' if config.use_volume_confirmation else 'OFF'} ({config.volume_multiplier}x) | RSI {'ON' if config.use_rsi_filter else 'OFF'} ({config.rsi_overbought}/{config.rsi_oversold})")
    max_mom_str = f"{config.max_momentum_pct}%" if config.max_momentum_pct > 0 else "OFF"
    min_atr_str = f"{config.min_atr_pct}%" if config.min_atr_pct > 0 else "OFF"
    max_atr_str = f"{config.max_atr_pct}%" if config.max_atr_pct > 0 else "OFF"
    ema_str = f"EMA{config.ema_period}(slope {config.ema_slope_candles}c)" if config.use_ema_trend else "OFF"
    blocked_str = str(config.blocked_hours) if config.blocked_hours else "OFF"
    print(f"    Extra:     MaxMom {max_mom_str} | ATR [{min_atr_str}-{max_atr_str}] | EMA Trend {ema_str}")
    print(f"    BlockHours:{blocked_str}")

    print(f"\n  ─── Tổng quan ───")
    print(f"    Tổng trades:     {len(trades)}")
    print(f"    Win / Loss:      {len(wins)} / {len(losses)}  ({len(wins)/len(trades)*100:.1f}% winrate)")
    print(f"    Long / Short:    {len(long_trades)} / {len(short_trades)}")

    print(f"\n  ─── PnL ───")
    print(f"    Tổng PnL:        {total_pnl:+.2f} USDT  ({total_pnl_pct:+.2f}% raw)")
    print(f"    Avg Win:         {avg_win:+.2f} USDT")
    print(f"    Avg Loss:        {avg_loss:+.2f} USDT")
    print(f"    Max Win:         {max_win:+.2f} USDT")
    print(f"    Max Loss:        {max_loss:+.2f} USDT")
    print(f"    Max Drawdown:    {max_dd:.2f} USDT")
    print(f"    Max Consec Loss: {max_consec_loss}")

    print(f"\n  ─── Close Reasons ───")
    print(f"    Stop Loss:       {len(sl_trades)}")
    print(f"    Take Profit:     {len(tp_trades)}")
    other = len(trades) - len(sl_trades) - len(tp_trades)
    if other > 0:
        print(f"    Other:           {other}")

    # TP extension stats
    tp_extended = [t for t in trades if t.tp_hit_count > 0]
    if tp_extended:
        avg_ext = sum(t.tp_hit_count for t in tp_extended) / len(tp_extended)
        max_ext = max(t.tp_hit_count for t in tp_extended)
        print(f"\n  ─── Trailing TP ───")
        print(f"    Trades with TP extend: {len(tp_extended)}")
        print(f"    Avg extensions:        {avg_ext:.1f}")
        print(f"    Max extensions:        {max_ext}")

    # Individual trades
    print(f"\n  ─── Chi tiết trades ───")
    print(f"  {'#':>3} {'Dir':>5} {'Entry':>10} {'Exit':>10} {'PnL%':>8} {'PnL$':>8} {'TP#':>4} {'Reason':>20} {'Time'}")
    print(f"  {'─'*3} {'─'*5} {'─'*10} {'─'*10} {'─'*8} {'─'*8} {'─'*4} {'─'*20} {'─'*20}")

    for i, t in enumerate(trades, 1):
        dir_emoji = "🟢" if t.direction == TradeDirection.LONG else "🔴"
        pnl_emoji = "✅" if t.pnl_pct > 0 else "❌"
        entry_time = t.entry_time.strftime("%m/%d %H:%M") if t.entry_time else ""
        print(
            f"  {i:>3} {dir_emoji}{t.direction.value:>4} "
            f"{t.entry_price:>10.4f} {t.close_price:>10.4f} "
            f"{t.pnl_pct:>+7.2f}% {t.pnl_usdt:>+7.2f}$ "
            f"{t.tp_hit_count:>3}x "
            f"{t.close_reason:>20} "
            f"{pnl_emoji} {entry_time}"
        )

    # Equity curve (simple text)
    print(f"\n  ─── Equity Curve ───")
    equity = 0
    eq_points = []
    for t in trades:
        equity += t.pnl_usdt
        eq_points.append(equity)

    if eq_points:
        min_eq = min(eq_points)
        max_eq = max(eq_points)
        range_eq = max_eq - min_eq if max_eq != min_eq else 1

        width = 40
        for i, eq in enumerate(eq_points):
            bar_pos = int((eq - min_eq) / range_eq * width)
            bar = "─" * bar_pos + "●"
            label = f"{eq:+.2f}"
            print(f"  T{i+1:>3} |{bar:<{width+1}} {label}")

    print("\n" + "=" * 70)


# ==================== Main ====================

def main():
    # Default file
    data_file = "powerusdt_15m_1000.json"

    if len(sys.argv) > 1:
        data_file = sys.argv[1]

    # Find data file
    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_path = os.path.join(script_dir, data_file)

    if not os.path.exists(data_path):
        print(f"❌ Không tìm thấy file: {data_path}")
        print(f"Files có sẵn:")
        for f in os.listdir(script_dir):
            if f.endswith("_15m_1000.json"):
                print(f"  - {f}")
        return

    # Load data
    print(f"\n📂 Loading: {data_file}")
    with open(data_path, 'r') as f:
        klines = json.load(f)

    # Detect symbol from filename
    symbol = data_file.split("_")[0].upper()
    print(f"📈 Symbol: {symbol}")

    # Load config from autotrade_config.json if exists
    config_path = os.path.join(script_dir, "autotrade_config.json")
    config = BacktestConfig()

    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                cfg = json.load(f)
            config.price_change_threshold = float(cfg.get("threshold", config.price_change_threshold))
            config.lookback_candles = int(cfg.get("lookback", config.lookback_candles))
            config.position_volume_usdt = float(cfg.get("size", config.position_volume_usdt))
            config.leverage = int(cfg.get("leverage", config.leverage))
            config.take_profit_pct = float(cfg.get("tp", config.take_profit_pct))
            config.stop_loss_pct = float(cfg.get("sl", config.stop_loss_pct))
            config.use_trailing_tp = cfg.get("trailing_tp", config.use_trailing_tp)
            config.tp_extension_pct = float(cfg.get("tp_extend", config.tp_extension_pct))
            config.max_tp_extensions = int(cfg.get("max_tp", config.max_tp_extensions))
            config.use_volume_confirmation = cfg.get("vol_confirm", config.use_volume_confirmation)
            config.volume_multiplier = float(cfg.get("vol_multiplier", config.volume_multiplier))
            config.use_rsi_filter = cfg.get("rsi_filter", config.use_rsi_filter)
            config.rsi_overbought = float(cfg.get("rsi_overbought", config.rsi_overbought))
            config.rsi_oversold = float(cfg.get("rsi_oversold", config.rsi_oversold))
            config.max_momentum_pct = float(cfg.get("max_momentum_pct", config.max_momentum_pct))
            config.min_atr_pct = float(cfg.get("min_atr_pct", config.min_atr_pct))
            config.max_atr_pct = float(cfg.get("max_atr_pct", config.max_atr_pct))
            bh = cfg.get("blocked_hours", None)
            config.blocked_hours = [int(h) for h in bh] if bh else None
            config.use_ema_trend = bool(cfg.get("use_ema_trend", config.use_ema_trend))
            config.ema_period = int(cfg.get("ema_period", config.ema_period))
            config.ema_slope_candles = int(cfg.get("ema_slope_candles", config.ema_slope_candles))
            print(f"⚙️  Config loaded from autotrade_config.json")
        except Exception as e:
            print(f"⚠️ Failed to load config, using defaults: {e}")

    # Run backtest
    print(f"\n🚀 Running backtest...")
    engine = BacktestEngine(config)
    trades = engine.run(klines)

    # Print report
    print_report(trades, config)


if __name__ == "__main__":
    main()

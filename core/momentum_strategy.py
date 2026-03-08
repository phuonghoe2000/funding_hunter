"""
Momentum Trading Strategy

Detects sudden price movements and enters trades in the direction of momentum.
- Price increases sharply → LONG
- Price decreases sharply → SHORT
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Callable, Any
from enum import Enum
from collections import deque

logger = logging.getLogger(__name__)


class TradeDirection(Enum):
    """Trade direction"""
    LONG = "long"
    SHORT = "short"
    NONE = "none"


class SignalStrength(Enum):
    """Signal strength levels"""
    WEAK = "weak"
    MEDIUM = "medium"
    STRONG = "strong"


@dataclass
class MomentumConfig:
    """Configuration for momentum strategy"""
    # Detection settings
    timeframe_seconds: int = 300  # 5 minutes default
    price_change_threshold: float = 1.0  # 1% change triggers signal
    lookback_candles: int = 5  # Number of price points to compare
    
    # Volume confirmation
    use_volume_confirmation: bool = True  # Enabled by default
    volume_multiplier: float = 2.0  # Volume must be 2x average to confirm
    volume_lookback_candles: int = 20  # Number of candles to calculate average volume
    
    # RSI filter - reject signals when RSI is overbought/oversold
    use_rsi_filter: bool = True  # Enabled by default
    rsi_period: int = 14  # RSI calculation period
    rsi_overbought: float = 70.0  # Reject LONG when RSI > this
    rsi_oversold: float = 30.0  # Reject SHORT when RSI < this
    
    # Position settings
    position_volume_usdt: float = 100.0  # Volume in USDT, size = floor(volume/price)
    leverage: int = 10
    take_profit_pct: float = 0.5  # 0.5% take profit
    stop_loss_pct: float = 0.3  # 0.3% stop loss
    
    # Trailing TP settings
    use_trailing_tp: bool = True  # Enable trailing take profit
    tp_extension_pct: float = 0.3  # Extend TP by this % when hit
    max_tp_extensions: int = 10  # Max number of TP extensions (0 = unlimited)
    
    # Extra signal filters
    max_momentum_pct: float = 0.0  # Skip if |momentum| > this (0 = disabled)
    min_atr_pct: float = 0.0       # Skip if ATR% < this (0 = disabled)
    use_ema_trend: bool = False     # Skip trades against EMA trend
    ema_period: int = 50            # EMA period for trend detection
    ema_slope_candles: int = 3      # Compare EMA now vs N candles ago

    # Risk management
    max_positions: int = 1  # Max concurrent positions
    cooldown_seconds: int = 60  # Wait time after closing position
    
    # Monitoring
    check_interval_seconds: float = 1.0  # Check price every 1 second


@dataclass
class PricePoint:
    """A single price data point"""
    price: float
    volume: float
    timestamp: datetime


@dataclass
class MomentumSignal:
    """Momentum signal data"""
    direction: TradeDirection
    strength: SignalStrength
    price_change_pct: float
    current_price: float
    trigger_price: float  # Price from lookback period
    volume_ratio: float  # Current volume / average volume
    rsi: float  # RSI value at signal time
    timestamp: datetime
    
    def to_dict(self) -> Dict:
        return {
            "direction": self.direction.value,
            "strength": self.strength.value,
            "price_change_pct": self.price_change_pct,
            "current_price": self.current_price,
            "trigger_price": self.trigger_price,
            "volume_ratio": self.volume_ratio,
            "rsi": self.rsi,
            "timestamp": self.timestamp.isoformat()
        }


@dataclass
class ActiveTrade:
    """Active trade information"""
    symbol: str
    direction: TradeDirection
    entry_price: float
    size: float
    leverage: int
    take_profit_price: float
    stop_loss_price: float
    entry_time: datetime
    order_id: Optional[str] = None
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    is_closed: bool = False
    close_price: Optional[float] = None
    close_time: Optional[datetime] = None
    close_reason: Optional[str] = None
    # Trailing TP tracking
    tp_hit_count: int = 0  # Number of times TP has been extended
    original_tp_price: float = 0.0  # Original TP price for reference
    highest_price: float = 0.0  # Highest price seen (for LONG)
    lowest_price: float = 0.0  # Lowest price seen (for SHORT)
    # TP/SL order IDs (for Binance orders)
    tp_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None


class PriceTracker:
    """Tracks price history for momentum detection"""
    
    def __init__(self, max_history: int = 1000):
        self.prices: deque = deque(maxlen=max_history)
        self.volumes: deque = deque(maxlen=max_history)
        
    def add_price(self, price: float, volume: float = 0.0, timestamp: Optional[datetime] = None):
        """Add new price point"""
        self.prices.append(PricePoint(
            price=price,
            volume=volume,
            timestamp=timestamp or datetime.now(timezone.utc)
        ))
    
    def get_price_change(self, lookback_seconds: int) -> Optional[float]:
        """Get price change % over lookback period"""
        if len(self.prices) < 2:
            return None
        
        now = datetime.now(timezone.utc)
        current_price = self.prices[-1].price
        
        # Find price from lookback_seconds ago
        for point in reversed(self.prices):
            age = (now - point.timestamp).total_seconds()
            if age >= lookback_seconds:
                old_price = point.price
                return ((current_price - old_price) / old_price) * 100
        
        # Not enough history
        return None
    
    def get_current_price(self) -> Optional[float]:
        """Get most recent price"""
        if self.prices:
            return self.prices[-1].price
        return None
    
    def get_average_volume(self, periods: int = 20) -> float:
        """Get average volume over last N periods"""
        if len(self.prices) < periods:
            return 0.0
        
        recent = list(self.prices)[-periods:]
        volumes = [p.volume for p in recent if p.volume > 0]
        
        if not volumes:
            return 0.0
        return sum(volumes) / len(volumes)
    
    def get_current_volume(self) -> float:
        """Get most recent volume"""
        if self.prices and self.prices[-1].volume > 0:
            return self.prices[-1].volume
        return 0.0


class MomentumStrategy:
    """
    Momentum Trading Strategy
    
    Monitors price movements and generates trading signals when
    significant momentum is detected.
    """
    
    def __init__(
        self,
        config: MomentumConfig,
        exchange_client,  # Any exchange client
        on_signal: Optional[Callable[[MomentumSignal], None]] = None,
        on_trade: Optional[Callable[[ActiveTrade, str], None]] = None,
        on_log: Optional[Callable[[str], None]] = None
    ):
        self.config = config
        self.exchange = exchange_client
        self.on_signal = on_signal or (lambda s: None)
        self.on_trade = on_trade or (lambda t, m: None)
        self.log = on_log or (lambda m: logger.info(m))
        
        # Price tracking per symbol
        self.trackers: Dict[str, PriceTracker] = {}
        
        # Active trades
        self.active_trades: Dict[str, ActiveTrade] = {}
        
        # State
        self._running = False
        self._monitor_task = None
        self._last_trade_time: Dict[str, datetime] = {}
        self._last_candle_open_ts: int = 0  # Track last candle we ran signal check on
        
        # Symbol being monitored
        self.symbol: Optional[str] = None
    
    async def start(self, symbol: str):
        """Start monitoring a symbol"""
        self.symbol = symbol
        self._running = True
        
        # Initialize tracker with enough capacity for lookback
        # lookback_candles * timeframe_seconds / check_interval = entries needed
        # E.g. 7 candles × 900s (15m) / 1s check = 6300 entries needed
        if symbol not in self.trackers:
            entries_needed = int(
                (self.config.lookback_candles * self.config.timeframe_seconds) 
                / self.config.check_interval_seconds
            ) + 200  # buffer
            max_history = max(1000, entries_needed)
            self.trackers[symbol] = PriceTracker(max_history=max_history)
            self.log(f"PriceTracker capacity: {max_history} (lookback {self.config.lookback_candles}×{self.config.timeframe_seconds}s)")
        
        # Fetch historical data to have enough lookback data
        await self._fetch_historical_data(symbol)
        
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self.log(f"Momentum strategy started for {symbol}")
    
    async def _fetch_historical_data(self, symbol: str):
        """Fetch historical klines to populate tracker with enough lookback data"""
        try:
            # Fetch enough candles for lookback + some buffer
            limit = self.config.lookback_candles + 20
            timeframe_map = {60: "1m", 300: "5m", 900: "15m", 1800: "30m", 3600: "1h"}
            interval = timeframe_map.get(self.config.timeframe_seconds, "5m")
            
            self.log(f"DEBUG: fetching klines for symbol={symbol}, interval={interval}, limit={limit}")
            klines = await self.exchange.get_klines(symbol, interval, limit)
            if klines:
                tracker = self.trackers[symbol]
                for k in klines:
                    # k is dict: k["close"], k["volume"], k["close_time"]
                    price = float(k["close"])
                    volume = float(k["volume"])
                    timestamp = datetime.fromtimestamp(k["close_time"] / 1000, tz=timezone.utc)
                    tracker.add_price(price, volume, timestamp)
                
                self.log(f"Loaded {len(klines)} historical candles for {symbol}")
            else:
                self.log(f"Warning: Could not fetch historical data for {symbol}")
        except Exception as e:
            self.log(f"Warning: Could not fetch historical data for {symbol}: {e}")
    
    async def stop(self):
        """Stop monitoring"""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        self.log("Momentum strategy stopped")
    
    async def _monitor_loop(self):
        """Main monitoring loop with hard timeout and heartbeat"""
        import time as _time
        last_heartbeat = _time.time()
        
        self.log(f"🟢 Monitor loop started for {self.symbol}")
        
        while self._running and self.symbol:
            try:
                # Hard timeout 30s: nếu BẤT KỲ request nào hang, cắt ngay và retry
                # aiohttp ClientTimeout không phải lúc nào cũng đáng tin khi server
                # giữ connection mở mà không gửi data
                await asyncio.wait_for(
                    self._monitor_one_cycle(),
                    timeout=30.0
                )
            except asyncio.TimeoutError:
                try:
                    self.log(f"⚠️ Monitor cycle timeout (30s) - Binance API có thể chậm, retrying...")
                except Exception:
                    logger.error("Monitor cycle timeout (30s)")
            except asyncio.CancelledError:
                break
            except Exception as e:
                try:
                    self.log(f"Error in monitor loop: {e}")
                except Exception:
                    logger.error(f"Error in monitor loop (log also failed): {e}")
                await asyncio.sleep(5)
                continue
            
            # Heartbeat mỗi 120s: log xác nhận task còn sống
            now = _time.time()
            if now - last_heartbeat > 120:
                last_heartbeat = now
                trades_count = len(self.active_trades)
                try:
                    self.log(f"💓 Monitor alive | {self.symbol} | Active trades: {trades_count}")
                except Exception:
                    pass
            
            await asyncio.sleep(self.config.check_interval_seconds)
        
        self.log(f"🔴 Monitor loop exited | running={self._running} | symbol={self.symbol}")
    
    async def _monitor_one_cycle(self):
        """Single cycle of the monitor loop - tách ra để wrap asyncio.wait_for()"""
        # Fetch current price
        price = await self._fetch_price(self.symbol)
        if price:
            tracker = self.trackers[self.symbol]
            tracker.add_price(price)
            
            # Check for signals ONLY on candle close (= khi nến mới vừa mở)
            # Sync với backtest: backtest check tại candle close, live cũng vậy
            if self.symbol not in self.active_trades:
                import time as _time_module
                now_ts = int(_time_module.time())
                candle_open_ts = (now_ts // self.config.timeframe_seconds) * self.config.timeframe_seconds
                
                if candle_open_ts > self._last_candle_open_ts:
                    self._last_candle_open_ts = candle_open_ts
                    candle_time_str = datetime.fromtimestamp(candle_open_ts, tz=timezone.utc).strftime('%H:%M')
                    self.log(f"🕯️ Nến mới {candle_time_str} UTC — checking signal...")
                    signal = await self._check_signal(self.symbol)
                    if signal and signal.direction != TradeDirection.NONE:
                        await self._handle_signal(signal)
            
            # Monitor active trades (mỗi giây vẫn check để TP/SL chính xác)
            await self._monitor_trades()
    
    async def _fetch_price(self, symbol: str) -> Optional[float]:
        """Fetch current price from exchange"""
        try:
            price = await self.exchange.get_mark_price(symbol)
            return price
        except Exception as e:
            logger.error(f"Error fetching price for {symbol}: {e}")
            return None
    
    @staticmethod
    def _calculate_rsi(close_prices: list, period: int = 14) -> float:
        """Calculate RSI from a list of close prices
        
        Uses the standard Wilder's smoothing method:
        1. Calculate price changes
        2. Separate gains and losses
        3. Average gains/losses over period using EMA smoothing
        4. RS = avg_gain / avg_loss, RSI = 100 - (100 / (1 + RS))
        
        Args:
            close_prices: List of close prices (oldest first)
            period: RSI period (default 14)
            
        Returns:
            RSI value (0-100). Returns 50.0 if not enough data.
        """
        if len(close_prices) < period + 1:
            return 50.0  # Neutral default
        
        # Calculate price changes
        changes = [close_prices[i] - close_prices[i - 1] for i in range(1, len(close_prices))]
        
        # First average: simple average of first 'period' changes
        gains = [max(c, 0) for c in changes[:period]]
        losses = [abs(min(c, 0)) for c in changes[:period]]
        
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        
        # Wilder's smoothing for remaining changes
        for c in changes[period:]:
            gain = max(c, 0)
            loss = abs(min(c, 0))
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
        
        if avg_loss == 0:
            return 100.0  # All gains, no losses
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    async def _fetch_rsi(self, symbol: str) -> float:
        """Fetch RSI from exchange klines
        
        Returns:
            RSI value (0-100). Returns 50.0 (neutral) on error.
        """
        try:
            # Need rsi_period + 1 data points minimum, fetch extra for accuracy
            limit = self.config.rsi_period + 10
            # PHẢI dùng đúng timeframe đang trade (15m), KHÔNG phải volume_candle_interval (1m)
            tf_map = {60: "1m", 300: "5m", 900: "15m", 1800: "30m", 3600: "1h"}
            interval = tf_map.get(self.config.timeframe_seconds, "5m")
            klines = await self.exchange.get_klines(
                symbol=symbol,
                interval=interval,
                limit=limit
            )
            
            if len(klines) < self.config.rsi_period + 1:
                logger.warning(f"Not enough klines for RSI: {len(klines)}")
                return 50.0
            
            close_prices = [k["close"] for k in klines]
            rsi = self._calculate_rsi(close_prices, self.config.rsi_period)
            
            logger.debug(f"RSI for {symbol}: {rsi:.1f}")
            return rsi
            
        except Exception as e:
            logger.warning(f"Error fetching RSI for {symbol}: {e}")
            return 50.0  # Neutral default on error
    
    async def _check_signal(self, symbol: str) -> Optional[MomentumSignal]:
        """Check if momentum signal is triggered"""
        tracker = self.trackers.get(symbol)
        if not tracker:
            return None
        
        # Get price change over lookback period (lookback_candles * timeframe_seconds)
        lookback_period = self.config.lookback_candles * self.config.timeframe_seconds
        price_change = tracker.get_price_change(lookback_period)
        if price_change is None:
            # Log periodically (not every 1s) to avoid flooding
            import time as _t
            now_t = _t.time()
            if not hasattr(self, '_last_nodata_log') or now_t - self._last_nodata_log > 30:
                self._last_nodata_log = now_t
                oldest_age = 0
                if tracker.prices:
                    oldest_age = (datetime.now(timezone.utc) - tracker.prices[0].timestamp).total_seconds()
                self.log(
                    f"⏳ Chưa đủ dữ liệu lookback: cần {lookback_period}s, có {oldest_age:.0f}s "
                    f"({len(tracker.prices)}/{tracker.prices.maxlen} entries)"
                )
            return None
        
        current_price = tracker.get_current_price()
        if not current_price:
            return None
        
        # Determine direction and strength
        direction = TradeDirection.NONE
        strength = SignalStrength.WEAK
        
        threshold = self.config.price_change_threshold
        
        if price_change >= threshold:
            direction = TradeDirection.LONG
            if price_change >= threshold * 2:
                strength = SignalStrength.STRONG
            elif price_change >= threshold * 1.5:
                strength = SignalStrength.MEDIUM
        elif price_change <= -threshold:
            direction = TradeDirection.SHORT
            if price_change <= -threshold * 2:
                strength = SignalStrength.STRONG
            elif price_change <= -threshold * 1.5:
                strength = SignalStrength.MEDIUM
        
        # Max momentum filter (skip mean-reversion extremes)
        if self.config.max_momentum_pct > 0 and abs(price_change) > self.config.max_momentum_pct:
            logger.debug(f"Max momentum filter: rejected, |change|={abs(price_change):.2f}% > {self.config.max_momentum_pct}%")
            return None

        # Fetch klines (shared for volume confirmation + ATR filter)
        import time
        klines_data = None
        if self.config.use_volume_confirmation or self.config.min_atr_pct > 0:
            try:
                tf_map = {60: "1m", 300: "5m", 900: "15m", 1800: "30m", 3600: "1h"}
                interval = tf_map.get(self.config.timeframe_seconds, "5m")
                limit = self.config.volume_lookback_candles + 16  # extra for ATR (14 candles)
                klines_data = await self.exchange.get_klines(symbol, interval, limit)
            except Exception as e:
                logger.warning(f"Klines fetch error for {symbol}: {e}")

        # Volume confirmation
        volume_ratio = 1.0
        if self.config.use_volume_confirmation:
            if klines_data and len(klines_data) >= 2:
                try:
                    now_ms = time.time() * 1000
                    if klines_data[-1].get("close_time", 0) > now_ms:
                        closed_candle = klines_data[-2]
                        avg_klines = klines_data[:-2]
                    else:
                        closed_candle = klines_data[-1]
                        avg_klines = klines_data[:-1]
                    current_vol = float(closed_candle.get("volume", 0))
                    avg_vol = sum(float(k.get("volume", 0)) for k in avg_klines) / len(avg_klines) if avg_klines else 1.0
                    volume_ratio = current_vol / avg_vol if avg_vol > 0 else 1.0
                    self.log(f"📊 Vol {symbol}: Closed={current_vol:.0f}, Avg={avg_vol:.0f} | Ratio: {volume_ratio:.2f}x (yêu cầu {self.config.volume_multiplier}x)")
                except Exception as e:
                    logger.warning(f"Volume check error for {symbol}: {e}")
            if volume_ratio < self.config.volume_multiplier:
                direction = TradeDirection.NONE

        # RSI filter (optional) - reject overbought LONG / oversold SHORT
        rsi = 50.0
        if self.config.use_rsi_filter and direction != TradeDirection.NONE:
            rsi = await self._fetch_rsi(symbol)
            if direction == TradeDirection.LONG and rsi > self.config.rsi_overbought:
                logger.debug(f"RSI filter: LONG rejected, RSI={rsi:.1f} > {self.config.rsi_overbought} (overbought)")
                direction = TradeDirection.NONE
            elif direction == TradeDirection.SHORT and rsi < self.config.rsi_oversold:
                logger.debug(f"RSI filter: SHORT rejected, RSI={rsi:.1f} < {self.config.rsi_oversold} (oversold)")
                direction = TradeDirection.NONE

        # Min ATR filter (skip flat/sideways markets)
        if self.config.min_atr_pct > 0 and direction != TradeDirection.NONE:
            if klines_data and len(klines_data) >= 15:
                atr_candles = klines_data[-15:-1]  # last 14 closed candles
                ranges = [float(k.get("high", 0)) - float(k.get("low", 0)) for k in atr_candles]
                atr = sum(ranges) / len(ranges) if ranges else 0
                atr_pct = atr / current_price * 100 if current_price > 0 else 0
                if atr_pct < self.config.min_atr_pct:
                    logger.debug(f"Min ATR filter: rejected, ATR%={atr_pct:.3f}% < {self.config.min_atr_pct}%")
                    direction = TradeDirection.NONE

        # EMA trend filter (skip trades against trend)
        if self.config.use_ema_trend and direction != TradeDirection.NONE:
            need = self.config.ema_period + self.config.ema_slope_candles + 2
            if klines_data and len(klines_data) >= need:
                try:
                    from backtest import calculate_ema
                    closes_live = [float(k.get("close", 0)) for k in klines_data]
                    sc = self.config.ema_slope_candles
                    p  = self.config.ema_period
                    ema_now  = calculate_ema(closes_live, p)
                    ema_prev = calculate_ema(closes_live[:-sc], p)
                    trending_up   = ema_now > ema_prev
                    trending_down = ema_now < ema_prev
                    if direction == TradeDirection.SHORT and trending_up:
                        logger.debug(f"EMA trend filter: SHORT rejected, EMA dốc lên ({ema_prev:.2f} -> {ema_now:.2f})")
                        direction = TradeDirection.NONE
                    elif direction == TradeDirection.LONG and trending_down:
                        logger.debug(f"EMA trend filter: LONG rejected, EMA dốc xuống ({ema_prev:.2f} -> {ema_now:.2f})")
                        direction = TradeDirection.NONE
                except Exception as e:
                    logger.warning(f"EMA trend filter error: {e}")

        if direction == TradeDirection.NONE:
            return None
        
        # Return if no signal
        
        # Calculate trigger price
        if len(tracker.prices) > 0:
            for point in reversed(list(tracker.prices)):
                age = (datetime.now(timezone.utc) - point.timestamp).total_seconds()
                if age >= self.config.timeframe_seconds:
                    trigger_price = point.price
                    break
            else:
                trigger_price = tracker.prices[0].price
        else:
            trigger_price = current_price
        
        return MomentumSignal(
            direction=direction,
            strength=strength,
            price_change_pct=price_change,
            current_price=current_price,
            trigger_price=trigger_price,
            volume_ratio=volume_ratio,
            rsi=rsi,
            timestamp=datetime.now(timezone.utc)
        )
    
    async def _handle_signal(self, signal: MomentumSignal):
        """Handle a momentum signal"""
        symbol = self.symbol
        if not symbol:
            return
        
        # Check cooldown
        # Dùng cooldown_seconds nếu set > 0, ngược lại tự tính từ timeframe để sync backtest (cooldown_candles=4)
        COOLDOWN_CANDLES = 4
        effective_cooldown = self.config.cooldown_seconds if self.config.cooldown_seconds > 0 else (COOLDOWN_CANDLES * self.config.timeframe_seconds)
        # Nếu cooldown_seconds nhỏ hơn 1 nến → tự động nâng lên 4 nến để khớp backtest
        min_cooldown = COOLDOWN_CANDLES * self.config.timeframe_seconds
        if effective_cooldown < min_cooldown:
            effective_cooldown = min_cooldown

        last_trade = self._last_trade_time.get(symbol)
        if last_trade:
            elapsed = (datetime.now(timezone.utc) - last_trade).total_seconds()
            if elapsed < effective_cooldown:
                remaining = effective_cooldown - elapsed
                self.log(f"⏸️ Cooldown: còn {remaining:.0f}s ({effective_cooldown/self.config.timeframe_seconds:.0f} nến)")
                return  # Still in cooldown
        
        # Check max positions
        if len(self.active_trades) >= self.config.max_positions:
            return
        
        # Check if already have position for this symbol
        if symbol in self.active_trades:
            return
        
        # Notify signal
        self.on_signal(signal)
        vol_info = f" | Vol: {signal.volume_ratio:.1f}x" if self.config.use_volume_confirmation else ""
        rsi_info = f" | RSI: {signal.rsi:.1f}" if self.config.use_rsi_filter else ""
        self.log(f"Signal: {signal.direction.value.upper()} | Change: {signal.price_change_pct:+.2f}% | Price: {signal.current_price}{vol_info}{rsi_info}")
        
        # Execute trade
        await self._execute_trade(signal)
    
    async def _execute_trade(self, signal: MomentumSignal):
        """Execute a trade based on signal"""
        symbol = self.symbol
        if not symbol:
            return
        
        direction = signal.direction
        price = signal.current_price
        
        # Calculate TP/SL prices
        from config.constants import Side
        if direction == TradeDirection.LONG:
            side = Side.LONG
            tp_price = price * (1 + self.config.take_profit_pct / 100)
            sl_price = price * (1 - self.config.stop_loss_pct / 100)
        else:
            side = Side.SHORT
            tp_price = price * (1 - self.config.take_profit_pct / 100)
            sl_price = price * (1 + self.config.stop_loss_pct / 100)
        
        # Calculate size: volume / price (float, not truncated to int)
        size = self.config.position_volume_usdt / price
        if size < 0.001:
            self.log(f"WARNING: Calculated size too small ({size:.6f}) (volume={self.config.position_volume_usdt} USDT, price={price}). Need more volume!")
            return
        
        # Round size to exchange's permitted precision limit
        if hasattr(self.exchange, 'get_step_size') and hasattr(self.exchange, '_round_to_tick'):
            step_size = await self.exchange.get_step_size(symbol)
            size = self.exchange._round_to_tick(size, step_size)
        elif hasattr(self.exchange, '_round_quantity'):
            size = self.exchange._round_quantity(symbol, size)
        else:
            size = round(size, 1) # Fallback rounding
            
        self.log(f"Executing {side.value.upper()} {symbol} | Size: {size} | TP: {tp_price:.2f} | SL: {sl_price:.2f}")
        
        try:
            # Place market order
            order = await self.exchange.place_market_order(
                symbol=symbol,
                side=side,
                size=size
            )
            
            if order:
                entry = order.avg_price if order.avg_price else price
                
                # Recalculate TP/SL based on actual entry price
                if direction == TradeDirection.LONG:
                    tp_price = entry * (1 + self.config.take_profit_pct / 100)
                    sl_price = entry * (1 - self.config.stop_loss_pct / 100)
                else:
                    tp_price = entry * (1 - self.config.take_profit_pct / 100)
                    sl_price = entry * (1 + self.config.stop_loss_pct / 100)
                
                # Create active trade record
                trade = ActiveTrade(
                    symbol=symbol,
                    direction=direction,
                    entry_price=entry,
                    size=size,
                    leverage=self.config.leverage,
                    take_profit_price=tp_price,
                    stop_loss_price=sl_price,
                    entry_time=datetime.now(timezone.utc),
                    order_id=order.order_id,
                    original_tp_price=tp_price,
                    highest_price=entry,
                    lowest_price=entry
                )
                
                # Place TP/SL orders on exchange
                await self._place_tp_sl_orders(trade)
                
                self.active_trades[symbol] = trade
                self._last_trade_time[symbol] = datetime.now(timezone.utc)
                
                self.on_trade(trade, "OPENED")
                self.log(f"Trade opened: {direction.value.upper()} @ {trade.entry_price:.2f} | TP: {tp_price:.2f} | SL: {sl_price:.2f}")
                
        except Exception as e:
            self.log(f"Failed to execute trade: {e}")
    
    async def _place_tp_sl_orders(self, trade: ActiveTrade):
        """Track TP/SL locally (monitored by app's _monitor_trades loop).
        
        Note: We no longer place SL/TP on Binance.
        Both TP and SL are monitored by the app to enable trailing TP logic
        and avoid exchange-side order issues.
        When hit, we close the position via MARKET order.
        """
        # SL and TP are NOT placed on Binance - monitored by app
        trade.sl_order_id = None
        trade.tp_order_id = None
        
        self.log(f"TP & SL monitored locally by app | Target TP: {trade.take_profit_price:.6f} | SL: {trade.stop_loss_price:.6f}")
    
    async def _update_sl_order(self, trade: ActiveTrade):
        """Update SL price locally (for trailing).
        
        Called when TP is hit and we need to move SL up to lock profit.
        Since we track locally, we just log it. The actual price update is
        done in _check_tp_sl_long / _check_tp_sl_short.
        """
        self.log(f"Local SL updated to lock profit: {trade.stop_loss_price:.6f}")
    
    async def _monitor_trades(self):
        """Monitor active trades for TP/SL with trailing TP support"""
        for symbol, trade in list(self.active_trades.items()):
            if trade.is_closed:
                continue
            
            try:
                # Get current price
                current_price = await self._fetch_price(symbol)
                if not current_price:
                    continue
                
                trade.current_price = current_price
                
                # Update highest/lowest price tracking
                if trade.direction == TradeDirection.LONG:
                    if current_price > trade.highest_price:
                        trade.highest_price = current_price
                else:
                    if current_price < trade.lowest_price or trade.lowest_price == 0:
                        trade.lowest_price = current_price
                
                # Calculate PnL
                if trade.direction == TradeDirection.LONG:
                    pnl_pct = ((current_price - trade.entry_price) / trade.entry_price) * 100
                else:
                    pnl_pct = ((trade.entry_price - current_price) / trade.entry_price) * 100
                
                trade.unrealized_pnl = pnl_pct
                
                # Check TP/SL with trailing logic
                if trade.direction == TradeDirection.LONG:
                    await self._check_tp_sl_long(trade, current_price)
                else:
                    await self._check_tp_sl_short(trade, current_price)
                        
            except Exception as e:
                logger.error(f"Error monitoring trade {symbol}: {e}")
    
    async def _check_tp_sl_long(self, trade: ActiveTrade, current_price: float):
        """Check TP/SL for LONG position with trailing TP"""
        # Periodic log to show app is tracking
        import time
        now = time.time()
        if not hasattr(trade, 'last_track_log') or now - trade.last_track_log > 10:
            trade.last_track_log = now
            self.log(f"🔎 Tracking LONG {trade.symbol} | Price: {current_price:.6f} | SL: {trade.stop_loss_price:.6f} | TP: {trade.take_profit_price:.6f}")
        
        # Check Stop Loss first (Local monitor)
        if current_price <= trade.stop_loss_price:
            reason = "STOP_LOSS" if trade.tp_hit_count == 0 else f"TRAILING_SL (TP hit {trade.tp_hit_count}x)"
            await self._close_trade(trade, reason)
            return
        
        # Check Take Profit (monitored by app for trailing)
        if current_price >= trade.take_profit_price:
            if self.config.use_trailing_tp:
                # Check if we can extend TP
                can_extend = (self.config.max_tp_extensions == 0 or 
                             trade.tp_hit_count < self.config.max_tp_extensions)
                
                if can_extend:
                    old_tp = trade.take_profit_price
                    old_sl = trade.stop_loss_price
                    
                    # Move SL to old TP (lock in profit from previous TP level)
                    trade.stop_loss_price = trade.take_profit_price
                    
                    # Extend TP further
                    trade.take_profit_price = current_price * (1 + self.config.tp_extension_pct / 100)
                    trade.tp_hit_count += 1
                    
                    self.log(
                        f"🎯 TP #{trade.tp_hit_count} hit! Trailing... "
                        f"New SL: {trade.stop_loss_price:.6f} (was {old_sl:.6f}) | "
                        f"New TP: {trade.take_profit_price:.6f} (was {old_tp:.6f})"
                    )
                    
                    # Update SL order on exchange (TP is monitored by app)
                    await self._update_sl_order(trade)
                    
                    # Notify UI of TP extension
                    self.on_trade(trade, f"TP_EXTENDED_{trade.tp_hit_count}")
                else:
                    # Max extensions reached, close at profit
                    await self._close_trade(trade, f"MAX_TP (hit {trade.tp_hit_count}x)")
            else:
                # No trailing, just close
                await self._close_trade(trade, "TAKE_PROFIT")
    
    async def _check_tp_sl_short(self, trade: ActiveTrade, current_price: float):
        """Check TP/SL for SHORT position with trailing TP"""
        # Periodic log to show app is tracking
        import time
        now = time.time()
        if not hasattr(trade, 'last_track_log') or now - trade.last_track_log > 10:
            trade.last_track_log = now
            self.log(f"🔎 Tracking SHORT {trade.symbol} | Price: {current_price:.6f} | SL: {trade.stop_loss_price:.6f} | TP: {trade.take_profit_price:.6f}")
            
        # Check Stop Loss first (Local monitor)
        if current_price >= trade.stop_loss_price:
            reason = "STOP_LOSS" if trade.tp_hit_count == 0 else f"TRAILING_SL (TP hit {trade.tp_hit_count}x)"
            await self._close_trade(trade, reason)
            return
        
        # Check Take Profit (monitored by app for trailing)
        if current_price <= trade.take_profit_price:
            if self.config.use_trailing_tp:
                # Check if we can extend TP
                can_extend = (self.config.max_tp_extensions == 0 or 
                             trade.tp_hit_count < self.config.max_tp_extensions)
                
                if can_extend:
                    old_tp = trade.take_profit_price
                    old_sl = trade.stop_loss_price
                    
                    # Move SL to old TP (lock in profit from previous TP level)
                    trade.stop_loss_price = trade.take_profit_price
                    
                    # Extend TP further (lower for short)
                    trade.take_profit_price = current_price * (1 - self.config.tp_extension_pct / 100)
                    trade.tp_hit_count += 1
                    
                    self.log(
                        f"🎯 TP #{trade.tp_hit_count} hit! Trailing... "
                        f"New SL: {trade.stop_loss_price:.6f} (was {old_sl:.6f}) | "
                        f"New TP: {trade.take_profit_price:.6f} (was {old_tp:.6f})"
                    )
                    
                    # Update SL order on exchange (TP is monitored by app)
                    await self._update_sl_order(trade)
                    
                    # Notify UI of TP extension
                    self.on_trade(trade, f"TP_EXTENDED_{trade.tp_hit_count}")
                else:
                    # Max extensions reached, close at profit
                    await self._close_trade(trade, f"MAX_TP (hit {trade.tp_hit_count}x)")
            else:
                # No trailing, just close
                await self._close_trade(trade, "TAKE_PROFIT")
    
    async def _close_trade(self, trade: ActiveTrade, reason: str):
        """Close an active trade"""
        symbol = trade.symbol
        
        # Prevent multiple close attempts
        if trade.is_closed:
            self.log(f"Trade already closed, skipping")
            if symbol in self.active_trades:
                del self.active_trades[symbol]
            return
        
        # Cancellation of TP/SL exchange orders removed since they are now fully local
        
        # Check if position still exists on exchange
        try:
            position = await self.exchange.get_position(symbol, force_rest=True)
            if not position or position.size == 0:
                # Position already closed (maybe by TP/SL on Binance)
                self.log(f"Position already closed on exchange | Reason: {reason}")
                trade.is_closed = True
                trade.close_time = datetime.now(timezone.utc)
                trade.close_reason = reason + "_AUTO"
                self.on_trade(trade, f"CLOSED_{reason}_AUTO")
                
                if symbol in self.active_trades:
                    del self.active_trades[symbol]
                return
        except Exception as e:
            self.log(f"Warning: Could not check position status: {e}")
            # Continue with close attempt
        
        # Determine close side and position_side for Hedge Mode
        # In Hedge Mode:
        #   - Close LONG: side=SELL (SHORT), positionSide=LONG
        #   - Close SHORT: side=BUY (LONG), positionSide=SHORT
        from config.constants import Side
        if trade.direction == TradeDirection.LONG:
            close_side = Side.SHORT  # SELL to close LONG
            position_side = "LONG"   # We're closing the LONG position
        else:
            close_side = Side.LONG   # BUY to close SHORT
            position_side = "SHORT"  # We're closing the SHORT position
        
        self.log(f"Closing trade: {reason} | PnL: {trade.unrealized_pnl:+.2f}%")
        
        try:
            order = await self.exchange.place_market_order(
                symbol=symbol,
                side=close_side,
                size=trade.size,
                position_side=position_side  # Critical for Hedge Mode!
            )
            
            if order:
                trade.is_closed = True
                trade.close_price = order.avg_price if order.avg_price else trade.current_price
                trade.close_time = datetime.now(timezone.utc)
                trade.close_reason = reason
                
                self.on_trade(trade, f"CLOSED_{reason}")
                self.log(f"Trade closed @ {trade.close_price:.2f} | Reason: {reason}")
                
                # Remove from active trades
                if symbol in self.active_trades:
                    del self.active_trades[symbol]
                    
        except Exception as e:
            error_msg = str(e)
            # -2022: ReduceOnly rejected = position already closed
            if "-2022" in error_msg:
                self.log(f"Position already closed (ReduceOnly rejected)")
                trade.is_closed = True
                trade.close_time = datetime.now(timezone.utc)
                trade.close_reason = reason + "_AUTO"
                
                if symbol in self.active_trades:
                    del self.active_trades[symbol]
            else:
                self.log(f"Failed to close trade: {e}")
    
    def get_status(self) -> Dict[str, Any]:
        """Get strategy status"""
        return {
            "running": self._running,
            "symbol": self.symbol,
            "active_trades": len(self.active_trades),
            "config": {
                "timeframe": self.config.timeframe_seconds,
                "threshold": self.config.price_change_threshold,
                "tp": self.config.take_profit_pct,
                "sl": self.config.stop_loss_pct
            }
        }


# Factory function
def create_momentum_strategy(
    exchange_client,
    **config_kwargs
) -> MomentumStrategy:
    """Create a MomentumStrategy with custom config"""
    config = MomentumConfig(**config_kwargs)
    return MomentumStrategy(config, exchange_client)

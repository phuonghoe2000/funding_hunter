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
    
    # Volume confirmation (optional)
    use_volume_confirmation: bool = False
    volume_multiplier: float = 2.0  # Volume must be 2x average
    
    # Position settings
    position_size_usdt: float = 100.0
    leverage: int = 10
    take_profit_pct: float = 0.5  # 0.5% take profit
    stop_loss_pct: float = 0.3  # 0.3% stop loss
    
    # Trailing TP settings
    use_trailing_tp: bool = True  # Enable trailing take profit
    tp_extension_pct: float = 0.3  # Extend TP by this % when hit
    max_tp_extensions: int = 10  # Max number of TP extensions (0 = unlimited)
    
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
    timestamp: datetime
    
    def to_dict(self) -> Dict:
        return {
            "direction": self.direction.value,
            "strength": self.strength.value,
            "price_change_pct": self.price_change_pct,
            "current_price": self.current_price,
            "trigger_price": self.trigger_price,
            "volume_ratio": self.volume_ratio,
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


class PriceTracker:
    """Tracks price history for momentum detection"""
    
    def __init__(self, max_history: int = 1000):
        self.prices: deque = deque(maxlen=max_history)
        self.volumes: deque = deque(maxlen=max_history)
        
    def add_price(self, price: float, volume: float = 0.0):
        """Add new price point"""
        self.prices.append(PricePoint(
            price=price,
            volume=volume,
            timestamp=datetime.now(timezone.utc)
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
        
        # Symbol being monitored
        self.symbol: Optional[str] = None
    
    async def start(self, symbol: str):
        """Start monitoring a symbol"""
        self.symbol = symbol
        self._running = True
        
        # Initialize tracker
        if symbol not in self.trackers:
            self.trackers[symbol] = PriceTracker()
        
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self.log(f"Momentum strategy started for {symbol}")
    
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
        """Main monitoring loop"""
        while self._running and self.symbol:
            try:
                # Fetch current price
                price = await self._fetch_price(self.symbol)
                if price:
                    tracker = self.trackers[self.symbol]
                    tracker.add_price(price)
                    
                    # Check for signals
                    signal = self._check_signal(self.symbol)
                    if signal and signal.direction != TradeDirection.NONE:
                        await self._handle_signal(signal)
                    
                    # Monitor active trades
                    await self._monitor_trades()
                
                await asyncio.sleep(self.config.check_interval_seconds)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log(f"Error in monitor loop: {e}")
                await asyncio.sleep(5)
    
    async def _fetch_price(self, symbol: str) -> Optional[float]:
        """Fetch current price from exchange"""
        try:
            price = await self.exchange.get_mark_price(symbol)
            return price
        except Exception as e:
            logger.error(f"Error fetching price for {symbol}: {e}")
            return None
    
    def _check_signal(self, symbol: str) -> Optional[MomentumSignal]:
        """Check if momentum signal is triggered"""
        tracker = self.trackers.get(symbol)
        if not tracker:
            return None
        
        # Get price change over timeframe
        price_change = tracker.get_price_change(self.config.timeframe_seconds)
        if price_change is None:
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
        
        # Volume confirmation (optional)
        volume_ratio = 1.0
        if self.config.use_volume_confirmation:
            avg_volume = tracker.get_average_volume()
            current_volume = tracker.get_current_volume()
            if avg_volume > 0:
                volume_ratio = current_volume / avg_volume
                if volume_ratio < self.config.volume_multiplier:
                    direction = TradeDirection.NONE  # Volume not confirmed
        
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
            timestamp=datetime.now(timezone.utc)
        )
    
    async def _handle_signal(self, signal: MomentumSignal):
        """Handle a momentum signal"""
        symbol = self.symbol
        if not symbol:
            return
        
        # Check cooldown
        last_trade = self._last_trade_time.get(symbol)
        if last_trade:
            elapsed = (datetime.now(timezone.utc) - last_trade).total_seconds()
            if elapsed < self.config.cooldown_seconds:
                return  # Still in cooldown
        
        # Check max positions
        if len(self.active_trades) >= self.config.max_positions:
            return
        
        # Check if already have position for this symbol
        if symbol in self.active_trades:
            return
        
        # Notify signal
        self.on_signal(signal)
        self.log(f"Signal: {signal.direction.value.upper()} | Change: {signal.price_change_pct:+.2f}% | Price: {signal.current_price}")
        
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
        if direction == TradeDirection.LONG:
            side = "BUY"
            tp_price = price * (1 + self.config.take_profit_pct / 100)
            sl_price = price * (1 - self.config.stop_loss_pct / 100)
        else:
            side = "SELL"
            tp_price = price * (1 - self.config.take_profit_pct / 100)
            sl_price = price * (1 + self.config.stop_loss_pct / 100)
        
        # Calculate size
        size = self.config.position_size_usdt / price
        
        self.log(f"Executing {side} {symbol} | Size: {size:.6f} | TP: {tp_price:.2f} | SL: {sl_price:.2f}")
        
        try:
            # Place market order
            order = await self.exchange.place_market_order(
                symbol=symbol,
                side=side,
                size=size
            )
            
            if order:
                entry = order.avg_price if order.avg_price else price
                
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
                
                self.active_trades[symbol] = trade
                self._last_trade_time[symbol] = datetime.now(timezone.utc)
                
                self.on_trade(trade, "OPENED")
                self.log(f"Trade opened: {direction.value.upper()} @ {trade.entry_price:.2f}")
                
                # Place TP/SL orders (if exchange supports)
                # await self._place_tp_sl_orders(trade)
                
        except Exception as e:
            self.log(f"Failed to execute trade: {e}")
    
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
        # Check Stop Loss first
        if current_price <= trade.stop_loss_price:
            reason = "STOP_LOSS" if trade.tp_hit_count == 0 else f"TRAILING_SL (TP hit {trade.tp_hit_count}x)"
            await self._close_trade(trade, reason)
            return
        
        # Check Take Profit
        if current_price >= trade.take_profit_price:
            if self.config.use_trailing_tp:
                # Check if we can extend TP
                can_extend = (self.config.max_tp_extensions == 0 or 
                             trade.tp_hit_count < self.config.max_tp_extensions)
                
                if can_extend:
                    # Move SL to current TP (lock profit)
                    old_tp = trade.take_profit_price
                    old_sl = trade.stop_loss_price
                    
                    trade.stop_loss_price = trade.take_profit_price
                    
                    # Extend TP by tp_extension_pct
                    trade.take_profit_price = current_price * (1 + self.config.tp_extension_pct / 100)
                    trade.tp_hit_count += 1
                    
                    self.log(
                        f"🎯 TP #{trade.tp_hit_count} hit! Trailing... "
                        f"New SL: {trade.stop_loss_price:.2f} (was {old_sl:.2f}) | "
                        f"New TP: {trade.take_profit_price:.2f} (was {old_tp:.2f})"
                    )
                    
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
        # Check Stop Loss first
        if current_price >= trade.stop_loss_price:
            reason = "STOP_LOSS" if trade.tp_hit_count == 0 else f"TRAILING_SL (TP hit {trade.tp_hit_count}x)"
            await self._close_trade(trade, reason)
            return
        
        # Check Take Profit
        if current_price <= trade.take_profit_price:
            if self.config.use_trailing_tp:
                # Check if we can extend TP
                can_extend = (self.config.max_tp_extensions == 0 or 
                             trade.tp_hit_count < self.config.max_tp_extensions)
                
                if can_extend:
                    # Move SL to current TP (lock profit)
                    old_tp = trade.take_profit_price
                    old_sl = trade.stop_loss_price
                    
                    trade.stop_loss_price = trade.take_profit_price
                    
                    # Extend TP by tp_extension_pct (lower for short)
                    trade.take_profit_price = current_price * (1 - self.config.tp_extension_pct / 100)
                    trade.tp_hit_count += 1
                    
                    self.log(
                        f"🎯 TP #{trade.tp_hit_count} hit! Trailing... "
                        f"New SL: {trade.stop_loss_price:.2f} (was {old_sl:.2f}) | "
                        f"New TP: {trade.take_profit_price:.2f} (was {old_tp:.2f})"
                    )
                    
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
        
        # Determine close side
        if trade.direction == TradeDirection.LONG:
            close_side = "SELL"
        else:
            close_side = "BUY"
        
        self.log(f"Closing trade: {reason} | PnL: {trade.unrealized_pnl:+.2f}%")
        
        try:
            order = await self.exchange.place_market_order(
                symbol=symbol,
                side=close_side,
                size=trade.size
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

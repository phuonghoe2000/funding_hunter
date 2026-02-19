"""
Scalping Strategy with DCA + RSI for BingX
Chiến lược: BingX "luôn thắng" bằng cách DCA thông minh khi drawdown

Logic:
- Mở LONG trên Binance, SHORT trên BingX (hoặc ngược lại)
- Khi giá đi ngược (BingX lỗ), DCA thêm trên BingX với size = base_size
- Khi giá hồi 0.5%, đóng phần DCA để chốt lãi
- RSI xác nhận tín hiệu trước khi DCA

DCA Levels: -0.3%, -0.6%, -1%, -1.5%, -2%
Multiplier: 1.0x (equal size mỗi lần)
Take Profit: 0.5% cho phần DCA
Max DCA: 3x vốn gốc (tức 3 lần DCA thêm)
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Callable, Any
from enum import Enum
import time

logger = logging.getLogger(__name__)


class TradingMode(Enum):
    """Trading mode selection"""
    FUNDING = "funding"  # Traditional funding arbitrage
    SCALPING = "scalping"  # Scalping with DCA


class DCALevel(Enum):
    """DCA trigger levels based on drawdown %"""
    LEVEL_1 = -0.3
    LEVEL_2 = -0.6
    LEVEL_3 = -1.0
    LEVEL_4 = -1.5
    LEVEL_5 = -2.0


@dataclass
class ScalpingConfig:
    """Configuration for scalping strategy"""
    # DCA Settings
    dca_levels: List[float] = field(default_factory=lambda: [-0.3, -0.6, -1.0, -1.5, -2.0])
    dca_multiplier: float = 1.0  # Equal size each DCA
    max_dca_times: int = 3  # Max 3x base size total DCA
    take_profit_pct: float = 0.5  # Take profit when recover 0.5%
    
    # RSI Settings
    rsi_period: int = 14
    rsi_timeframe: str = "5m"
    rsi_oversold: int = 30  # DCA for LONG when RSI < 30
    rsi_overbought: int = 70  # DCA for SHORT when RSI > 70
    use_rsi_confirmation: bool = True  # Require RSI confirmation for DCA
    
    # Risk Management
    max_drawdown_pct: float = 5.0  # Stop loss at -5%
    check_interval_seconds: float = 5.0
    
    # Notification
    notify_on_signal: bool = True
    notify_on_dca: bool = True
    notify_on_take_profit: bool = True


@dataclass
class DCAOrder:
    """Represents a DCA order"""
    level: int  # 1-5
    entry_price: float
    size: float
    side: str  # "LONG" or "SHORT"
    timestamp: datetime
    pnl: float = 0.0
    is_closed: bool = False
    close_price: Optional[float] = None


@dataclass 
class ScalpingPosition:
    """
    Represents a scalping position with DCA orders
    """
    # Base position info
    pair: str
    base_size: float
    leverage: int
    
    # Exchange assignment
    bingx_side: str  # "LONG" or "SHORT" - BingX side (the winning side)
    binance_side: str  # opposite of bingx_side
    
    # Entry prices
    bingx_entry: float = 0.0
    binance_entry: float = 0.0
    
    # DCA tracking
    dca_orders: List[DCAOrder] = field(default_factory=list)
    dca_count: int = 0
    last_dca_level: int = 0
    total_dca_size: float = 0.0
    
    # PnL tracking
    current_price: float = 0.0
    bingx_pnl: float = 0.0
    binance_pnl: float = 0.0
    total_pnl: float = 0.0
    drawdown_pct: float = 0.0
    
    # RSI
    current_rsi: float = 50.0
    
    # Status
    is_active: bool = True
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def get_total_bingx_size(self) -> float:
        """Get total position size on BingX (base + DCA)"""
        active_dca_size = sum(o.size for o in self.dca_orders if not o.is_closed)
        return self.base_size + active_dca_size
    
    def get_avg_entry_price(self) -> float:
        """Get average entry price including DCA"""
        if not self.dca_orders:
            return self.bingx_entry
        
        total_value = self.bingx_entry * self.base_size
        total_size = self.base_size
        
        for order in self.dca_orders:
            if not order.is_closed:
                total_value += order.entry_price * order.size
                total_size += order.size
        
        return total_value / total_size if total_size > 0 else self.bingx_entry


class RSICalculator:
    """Calculate RSI indicator"""
    
    def __init__(self, period: int = 14):
        self.period = period
        self.prices: List[float] = []
        self.gains: List[float] = []
        self.losses: List[float] = []
        self.avg_gain: float = 0
        self.avg_loss: float = 0
    
    def add_price(self, price: float) -> Optional[float]:
        """Add new price and return RSI if enough data"""
        self.prices.append(price)
        
        if len(self.prices) < 2:
            return None
        
        # Calculate price change
        change = self.prices[-1] - self.prices[-2]
        
        if change > 0:
            self.gains.append(change)
            self.losses.append(0)
        else:
            self.gains.append(0)
            self.losses.append(abs(change))
        
        # Need at least period + 1 prices
        if len(self.prices) < self.period + 1:
            return None
        
        # Calculate RSI
        if len(self.gains) == self.period:
            # First calculation: simple average
            self.avg_gain = sum(self.gains) / self.period
            self.avg_loss = sum(self.losses) / self.period
        else:
            # Subsequent: smoothed average
            self.avg_gain = (self.avg_gain * (self.period - 1) + self.gains[-1]) / self.period
            self.avg_loss = (self.avg_loss * (self.period - 1) + self.losses[-1]) / self.period
        
        if self.avg_loss == 0:
            return 100.0
        
        rs = self.avg_gain / self.avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    def reset(self):
        """Reset calculator"""
        self.prices.clear()
        self.gains.clear()
        self.losses.clear()
        self.avg_gain = 0
        self.avg_loss = 0


class ScalpingStrategy:
    """
    Scalping Strategy Manager
    
    Quản lý chiến lược DCA scalping cho BingX vs Binance
    """
    
    def __init__(
        self, 
        config: ScalpingConfig,
        bingx_client,  # BingXClient
        binance_client,  # BinanceClient
        on_notification: Optional[Callable[[str, str, Dict], None]] = None
    ):
        """
        Args:
            config: Scalping configuration
            bingx_client: BingX exchange client
            binance_client: Binance exchange client
            on_notification: Callback(type, message, data) for notifications
        """
        self.config = config
        self.bingx = bingx_client
        self.binance = binance_client
        self.notify = on_notification or self._default_notify
        
        # Position tracking
        self.positions: Dict[str, ScalpingPosition] = {}
        
        # RSI calculators per symbol
        self.rsi_calculators: Dict[str, RSICalculator] = {}
        
        # Running state
        self._running = False
        self._monitor_task = None
    
    def _default_notify(self, msg_type: str, message: str, data: Dict):
        """Default notification handler - just log"""
        logger.info(f"[{msg_type}] {message}")
    
    async def start(self):
        """Start monitoring"""
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("Scalping strategy started")
    
    async def stop(self):
        """Stop monitoring"""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("Scalping strategy stopped")
    
    async def open_position(
        self,
        pair: str,
        size: float,
        leverage: int,
        bingx_side: str = "SHORT"  # BingX default SHORT (profit when price drops)
    ) -> Optional[ScalpingPosition]:
        """
        Open a new scalping position
        
        Args:
            pair: Trading pair (e.g., "BTC/USDT")
            size: Base position size
            leverage: Leverage to use
            bingx_side: Side for BingX ("LONG" or "SHORT")
        
        Returns:
            ScalpingPosition if successful
        """
        if pair in self.positions:
            logger.warning(f"Position already exists for {pair}")
            return None
        
        binance_side = "LONG" if bingx_side == "SHORT" else "SHORT"
        
        # Create position object
        position = ScalpingPosition(
            pair=pair,
            base_size=size,
            leverage=leverage,
            bingx_side=bingx_side,
            binance_side=binance_side
        )
        
        # Initialize RSI calculator
        self.rsi_calculators[pair] = RSICalculator(self.config.rsi_period)
        
        self.positions[pair] = position
        
        # Notify
        self.notify("POSITION_OPENED", f"Opened {pair} position", {
            "pair": pair,
            "size": size,
            "bingx_side": bingx_side,
            "binance_side": binance_side,
            "leverage": leverage
        })
        
        return position
    
    async def _monitor_loop(self):
        """Main monitoring loop"""
        while self._running:
            try:
                for pair, position in list(self.positions.items()):
                    if not position.is_active:
                        continue
                    
                    await self._update_position(pair, position)
                    await self._check_dca_signals(pair, position)
                    await self._check_take_profit(pair, position)
                    await self._check_stop_loss(pair, position)
                
                await asyncio.sleep(self.config.check_interval_seconds)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in monitor loop: {e}")
                await asyncio.sleep(5)
    
    async def _update_position(self, pair: str, position: ScalpingPosition):
        """Update position data from exchanges"""
        try:
            # Get current prices
            bingx_symbol = self._get_bingx_symbol(pair)
            binance_symbol = self._get_binance_symbol(pair)
            
            # Fetch prices in parallel
            bingx_price_task = self.bingx.get_mark_price(bingx_symbol)
            binance_price_task = self.binance.get_mark_price(binance_symbol)
            
            bingx_price, binance_price = await asyncio.gather(
                bingx_price_task, binance_price_task,
                return_exceptions=True
            )
            
            if isinstance(bingx_price, Exception):
                logger.error(f"Error getting BingX price: {bingx_price}")
                return
            if isinstance(binance_price, Exception):
                logger.error(f"Error getting Binance price: {binance_price}")
                return
            
            # Use average price
            position.current_price = (bingx_price + binance_price) / 2
            
            # Update RSI
            rsi_calc = self.rsi_calculators.get(pair)
            if rsi_calc:
                rsi = rsi_calc.add_price(position.current_price)
                if rsi is not None:
                    position.current_rsi = rsi
            
            # Calculate PnL
            self._calculate_pnl(position)
            
        except Exception as e:
            logger.error(f"Error updating position {pair}: {e}")
    
    def _calculate_pnl(self, position: ScalpingPosition):
        """Calculate current PnL for position"""
        if position.bingx_entry == 0 or position.binance_entry == 0:
            return
        
        price = position.current_price
        
        # BingX PnL
        if position.bingx_side == "LONG":
            bingx_base_pnl = (price - position.bingx_entry) / position.bingx_entry * 100
        else:
            bingx_base_pnl = (position.bingx_entry - price) / position.bingx_entry * 100
        
        # Binance PnL (opposite side)
        if position.binance_side == "LONG":
            binance_pnl = (price - position.binance_entry) / position.binance_entry * 100
        else:
            binance_pnl = (position.binance_entry - price) / position.binance_entry * 100
        
        # Include DCA orders in BingX PnL
        dca_pnl = 0.0
        for order in position.dca_orders:
            if not order.is_closed:
                if position.bingx_side == "LONG":
                    order.pnl = (price - order.entry_price) / order.entry_price * 100
                else:
                    order.pnl = (order.entry_price - price) / order.entry_price * 100
                dca_pnl += order.pnl * (order.size / position.base_size)
        
        # Weight BingX PnL by position sizes
        total_bingx_size = position.get_total_bingx_size()
        weighted_bingx_pnl = (bingx_base_pnl * position.base_size + dca_pnl * position.total_dca_size) / total_bingx_size if total_bingx_size > 0 else bingx_base_pnl
        
        position.bingx_pnl = weighted_bingx_pnl
        position.binance_pnl = binance_pnl
        position.total_pnl = weighted_bingx_pnl + binance_pnl
        position.drawdown_pct = min(position.total_pnl, 0)
    
    async def _check_dca_signals(self, pair: str, position: ScalpingPosition):
        """Check if DCA conditions are met"""
        # Check max DCA reached
        if position.dca_count >= self.config.max_dca_times:
            return
        
        # Check if we're at a new DCA level
        current_level = self._get_dca_level(position.drawdown_pct)
        
        if current_level <= position.last_dca_level:
            return  # Already DCA'd at this level
        
        # Check RSI confirmation if enabled
        rsi_confirmed = True
        if self.config.use_rsi_confirmation:
            rsi_confirmed = self._check_rsi_for_dca(position)
        
        if not rsi_confirmed:
            # Notify signal without RSI confirmation
            if self.config.notify_on_signal:
                self.notify("DCA_SIGNAL", 
                    f"{pair}: DCA Level {current_level} triggered but RSI not confirmed", {
                    "pair": pair,
                    "drawdown": position.drawdown_pct,
                    "rsi": position.current_rsi,
                    "level": current_level
                })
            return
        
        # DCA signal confirmed!
        await self._execute_dca(pair, position, current_level)
    
    def _get_dca_level(self, drawdown_pct: float) -> int:
        """Get current DCA level based on drawdown"""
        level = 0
        for i, threshold in enumerate(self.config.dca_levels):
            if drawdown_pct <= threshold:
                level = i + 1
        return level
    
    def _check_rsi_for_dca(self, position: ScalpingPosition) -> bool:
        """Check if RSI confirms DCA signal"""
        rsi = position.current_rsi
        
        # For BingX SHORT: DCA when RSI is overbought (price likely to fall)
        if position.bingx_side == "SHORT":
            return rsi >= self.config.rsi_overbought
        
        # For BingX LONG: DCA when RSI is oversold (price likely to rise)
        else:
            return rsi <= self.config.rsi_oversold
    
    async def _execute_dca(self, pair: str, position: ScalpingPosition, level: int):
        """Execute DCA order (notification only - no actual trade)"""
        dca_size = position.base_size * self.config.dca_multiplier
        
        # Create DCA order record
        dca_order = DCAOrder(
            level=level,
            entry_price=position.current_price,
            size=dca_size,
            side=position.bingx_side,
            timestamp=datetime.now(timezone.utc)
        )
        
        # Update position
        position.dca_orders.append(dca_order)
        position.dca_count += 1
        position.last_dca_level = level
        position.total_dca_size += dca_size
        
        # Notify
        if self.config.notify_on_dca:
            self.notify("DCA_EXECUTE", f"{pair}: DCA Level {level} - Execute NOW!", {
                "pair": pair,
                "level": level,
                "size": dca_size,
                "entry_price": position.current_price,
                "total_dca_size": position.total_dca_size,
                "rsi": position.current_rsi,
                "action": "EXECUTE DCA",
                "bingx_order": {
                    "side": position.bingx_side,
                    "size": dca_size
                },
                "binance_order": {
                    "side": position.binance_side,
                    "size": dca_size
                }
            })
    
    async def _check_take_profit(self, pair: str, position: ScalpingPosition):
        """Check if DCA orders should be closed for profit"""
        for order in position.dca_orders:
            if order.is_closed:
                continue
            
            # Check if this DCA order is profitable enough
            if order.pnl >= self.config.take_profit_pct:
                await self._close_dca_order(pair, position, order)
    
    async def _close_dca_order(self, pair: str, position: ScalpingPosition, order: DCAOrder):
        """Close a DCA order (notification only)"""
        order.is_closed = True
        order.close_price = position.current_price
        
        # Calculate profit
        profit_pct = order.pnl
        profit_usd = order.size * order.entry_price * (profit_pct / 100)
        
        if self.config.notify_on_take_profit:
            self.notify("TAKE_PROFIT", f"{pair}: DCA Level {order.level} take profit!", {
                "pair": pair,
                "level": order.level,
                "entry_price": order.entry_price,
                "close_price": position.current_price,
                "profit_pct": profit_pct,
                "profit_usd": profit_usd,
                "action": "CLOSE DCA POSITION",
                "bingx_order": {
                    "action": "CLOSE",
                    "side": "LONG" if position.bingx_side == "SHORT" else "SHORT",
                    "size": order.size
                },
                "binance_order": {
                    "action": "CLOSE",
                    "side": "LONG" if position.binance_side == "SHORT" else "SHORT",
                    "size": order.size
                }
            })
    
    async def _check_stop_loss(self, pair: str, position: ScalpingPosition):
        """Check if position should be stopped"""
        if position.total_pnl <= -self.config.max_drawdown_pct:
            self.notify("STOP_LOSS", f"{pair}: Stop loss triggered at {position.total_pnl:.2f}%", {
                "pair": pair,
                "total_pnl": position.total_pnl,
                "action": "CLOSE ALL POSITIONS",
                "bingx_size": position.get_total_bingx_size(),
                "binance_size": position.base_size
            })
            position.is_active = False
    
    def _get_bingx_symbol(self, pair: str) -> str:
        """Convert pair to BingX symbol format"""
        # BTC/USDT -> BTC-USDT
        return pair.replace("/", "-")
    
    def _get_binance_symbol(self, pair: str) -> str:
        """Convert pair to Binance symbol format"""
        # BTC/USDT -> BTCUSDT
        return pair.replace("/", "")
    
    def get_position(self, pair: str) -> Optional[ScalpingPosition]:
        """Get position for pair"""
        return self.positions.get(pair)
    
    def get_all_positions(self) -> Dict[str, ScalpingPosition]:
        """Get all positions"""
        return self.positions.copy()
    
    def remove_position(self, pair: str):
        """Remove position from tracking"""
        if pair in self.positions:
            del self.positions[pair]
        if pair in self.rsi_calculators:
            del self.rsi_calculators[pair]
    
    def get_status(self) -> Dict[str, Any]:
        """Get strategy status"""
        active_positions = sum(1 for p in self.positions.values() if p.is_active)
        total_pnl = sum(p.total_pnl for p in self.positions.values())
        total_dca = sum(p.dca_count for p in self.positions.values())
        
        return {
            "running": self._running,
            "active_positions": active_positions,
            "total_positions": len(self.positions),
            "total_pnl": total_pnl,
            "total_dca_orders": total_dca,
            "config": {
                "dca_levels": self.config.dca_levels,
                "take_profit": self.config.take_profit_pct,
                "max_dca": self.config.max_dca_times,
                "rsi_period": self.config.rsi_period
            }
        }


# Utility function to create strategy with default config
def create_scalping_strategy(
    bingx_client,
    binance_client,
    on_notification=None,
    **config_overrides
) -> ScalpingStrategy:
    """
    Factory function to create ScalpingStrategy with custom config
    
    Args:
        bingx_client: BingX client
        binance_client: Binance client
        on_notification: Notification callback
        **config_overrides: Override default config values
    
    Returns:
        ScalpingStrategy instance
    """
    config = ScalpingConfig(**config_overrides)
    return ScalpingStrategy(config, bingx_client, binance_client, on_notification)

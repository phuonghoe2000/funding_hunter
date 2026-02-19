"""
Scalping Bot - Main controller for BingX Scalping Strategy

This bot:
1. Monitors existing hedge positions (BingX vs Binance)
2. Detects DCA opportunities based on drawdown + RSI
3. Sends notifications (does not auto-trade)
4. Tracks performance and PnL
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, List, Callable, Any
from dataclasses import dataclass, field
from enum import Enum
import json

from .scalping_strategy import (
    ScalpingStrategy, 
    ScalpingConfig, 
    ScalpingPosition,
    RSICalculator,
    DCAOrder
)

logger = logging.getLogger(__name__)


class NotificationType(Enum):
    """Types of notifications"""
    INFO = "INFO"
    DCA_SIGNAL = "DCA_SIGNAL"
    DCA_EXECUTE = "DCA_EXECUTE"
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass
class Notification:
    """Notification message"""
    type: NotificationType
    title: str
    message: str
    data: Dict[str, Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> Dict:
        return {
            "type": self.type.value,
            "title": self.title,
            "message": self.message,
            "data": self.data,
            "timestamp": self.timestamp.isoformat()
        }


class NotificationManager:
    """
    Manages notifications - stores history and dispatches to callbacks
    """
    
    def __init__(self, max_history: int = 100):
        self.history: List[Notification] = []
        self.max_history = max_history
        self.callbacks: List[Callable[[Notification], None]] = []
    
    def add_callback(self, callback: Callable[[Notification], None]):
        """Add notification callback"""
        self.callbacks.append(callback)
    
    def remove_callback(self, callback: Callable[[Notification], None]):
        """Remove notification callback"""
        if callback in self.callbacks:
            self.callbacks.remove(callback)
    
    def notify(self, msg_type: str, message: str, data: Optional[Dict[str, Any]] = None):
        """Send notification"""
        try:
            notif_type = NotificationType[msg_type] if msg_type in NotificationType.__members__ else NotificationType.INFO
        except:
            notif_type = NotificationType.INFO
        
        notification = Notification(
            type=notif_type,
            title=msg_type.replace("_", " ").title(),
            message=message,
            data=data or {}
        )
        
        # Store in history
        self.history.append(notification)
        if len(self.history) > self.max_history:
            self.history.pop(0)
        
        # Dispatch to callbacks
        for callback in self.callbacks:
            try:
                callback(notification)
            except Exception as e:
                logger.error(f"Error in notification callback: {e}")
    
    def get_history(self, limit: int = 50, msg_type: Optional[str] = None) -> List[Notification]:
        """Get notification history"""
        history = self.history.copy()
        
        if msg_type:
            history = [n for n in history if n.type.value == msg_type]
        
        return history[-limit:]
    
    def clear_history(self):
        """Clear notification history"""
        self.history.clear()


class ScalpingBot:
    """
    Main Scalping Bot Controller
    
    Integrates:
    - Exchange clients (BingX, Binance)
    - ScalpingStrategy (DCA + RSI logic)
    - NotificationManager (alerts)
    - Position tracking
    """
    
    def __init__(
        self,
        bingx_client,
        binance_client,
        config: Optional[ScalpingConfig] = None
    ):
        """
        Initialize Scalping Bot
        
        Args:
            bingx_client: BingX exchange client
            binance_client: Binance exchange client
            config: Scalping configuration (uses default if None)
        """
        self.bingx = bingx_client
        self.binance = binance_client
        self.config = config or ScalpingConfig()
        
        # Notification manager
        self.notifications = NotificationManager()
        
        # Strategy
        self.strategy = ScalpingStrategy(
            config=self.config,
            bingx_client=bingx_client,
            binance_client=binance_client,
            on_notification=self.notifications.notify
        )
        
        # State
        self._running = False
        self._sync_task = None
        
        # Performance tracking
        self.stats = {
            "total_signals": 0,
            "total_dca": 0,
            "total_take_profit": 0,
            "total_stop_loss": 0,
            "realized_pnl": 0.0,
            "started_at": None
        }
    
    async def start(self):
        """Start the bot"""
        if self._running:
            logger.warning("Bot already running")
            return
        
        self._running = True
        self.stats["started_at"] = datetime.now(timezone.utc)
        
        # Start strategy monitoring
        await self.strategy.start()
        
        # Start position sync task
        self._sync_task = asyncio.create_task(self._sync_positions_loop())
        
        self.notifications.notify("INFO", "Scalping Bot started", {
            "config": {
                "dca_levels": self.config.dca_levels,
                "take_profit": self.config.take_profit_pct,
                "max_dca": self.config.max_dca_times
            }
        })
        
        logger.info("Scalping Bot started")
    
    async def stop(self):
        """Stop the bot"""
        self._running = False
        
        await self.strategy.stop()
        
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        
        self.notifications.notify("INFO", "Scalping Bot stopped", self.get_stats())
        logger.info("Scalping Bot stopped")
    
    async def _sync_positions_loop(self):
        """Periodically sync positions from exchanges"""
        while self._running:
            try:
                await self._sync_positions()
                await asyncio.sleep(30)  # Sync every 30 seconds
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error syncing positions: {e}")
                await asyncio.sleep(10)
    
    async def _sync_positions(self):
        """Sync positions from exchanges to strategy"""
        try:
            # Get positions from both exchanges
            bingx_positions = await self.bingx.get_all_positions()
            binance_positions = await self.binance.get_all_positions()
            
            # Build map of symbols to positions
            bingx_map = {self._normalize_symbol(p.symbol): p for p in bingx_positions}
            binance_map = {self._normalize_symbol(p.symbol): p for p in binance_positions}
            
            # Find hedged pairs (positions on both exchanges)
            common_symbols = set(bingx_map.keys()) & set(binance_map.keys())
            
            for symbol in common_symbols:
                bingx_pos = bingx_map[symbol]
                binance_pos = binance_map[symbol]
                
                # Check if hedged (opposite sides)
                if bingx_pos.side != binance_pos.side:
                    # This is a hedged position
                    pair = symbol.replace("-", "/").replace("USDT", "/USDT")
                    
                    if pair not in self.strategy.positions:
                        # Auto-detect and add to tracking
                        await self._add_detected_position(pair, bingx_pos, binance_pos)
                    else:
                        # Update existing position
                        self._update_position_from_exchange(pair, bingx_pos, binance_pos)
            
        except Exception as e:
            logger.error(f"Error in position sync: {e}")
    
    async def _add_detected_position(self, pair: str, bingx_pos, binance_pos):
        """Add a detected hedged position to tracking"""
        position = ScalpingPosition(
            pair=pair,
            base_size=bingx_pos.size,
            leverage=bingx_pos.leverage,
            bingx_side=bingx_pos.side.value if hasattr(bingx_pos.side, 'value') else str(bingx_pos.side),
            binance_side=binance_pos.side.value if hasattr(binance_pos.side, 'value') else str(binance_pos.side),
            bingx_entry=bingx_pos.entry_price,
            binance_entry=binance_pos.entry_price,
            current_price=bingx_pos.mark_price
        )
        
        self.strategy.positions[pair] = position
        self.strategy.rsi_calculators[pair] = RSICalculator(self.config.rsi_period)
        
        self.notifications.notify("INFO", f"Detected hedged position: {pair}", {
            "pair": pair,
            "bingx_side": position.bingx_side,
            "binance_side": position.binance_side,
            "size": position.base_size
        })
    
    def _update_position_from_exchange(self, pair: str, bingx_pos, binance_pos):
        """Update strategy position with latest exchange data"""
        position = self.strategy.positions.get(pair)
        if not position:
            return
        
        position.current_price = bingx_pos.mark_price
        position.bingx_pnl = bingx_pos.unrealized_pnl
        position.binance_pnl = binance_pos.unrealized_pnl
        position.total_pnl = bingx_pos.unrealized_pnl + binance_pos.unrealized_pnl
    
    def _normalize_symbol(self, symbol: str) -> str:
        """Normalize symbol for comparison"""
        # BTC-USDT, BTCUSDT -> BTCUSDT
        return symbol.replace("-", "").replace("_", "").upper()
    
    # === Manual Controls ===
    
    async def add_position(
        self,
        pair: str,
        size: float,
        leverage: int,
        bingx_side: str = "SHORT"
    ) -> Optional[ScalpingPosition]:
        """
        Manually add a position to track
        
        Args:
            pair: Trading pair (e.g., "BTC/USDT")
            size: Position size
            leverage: Leverage used
            bingx_side: Side on BingX ("LONG" or "SHORT")
        
        Returns:
            ScalpingPosition if successful
        """
        return await self.strategy.open_position(pair, size, leverage, bingx_side)
    
    def remove_position(self, pair: str):
        """Remove position from tracking"""
        self.strategy.remove_position(pair)
        self.notifications.notify("INFO", f"Removed position: {pair}", {"pair": pair})
    
    def get_position(self, pair: str) -> Optional[ScalpingPosition]:
        """Get position by pair"""
        return self.strategy.get_position(pair)
    
    def get_all_positions(self) -> Dict[str, ScalpingPosition]:
        """Get all tracked positions"""
        return self.strategy.get_all_positions()
    
    # === Callbacks ===
    
    def add_notification_callback(self, callback: Callable[[Notification], None]):
        """Add callback for notifications"""
        self.notifications.add_callback(callback)
    
    def remove_notification_callback(self, callback: Callable[[Notification], None]):
        """Remove notification callback"""
        self.notifications.remove_callback(callback)
    
    # === Stats & Status ===
    
    def get_stats(self) -> Dict[str, Any]:
        """Get bot statistics"""
        # Count notifications by type
        for notif in self.notifications.history:
            if notif.type == NotificationType.DCA_SIGNAL:
                self.stats["total_signals"] += 1
            elif notif.type == NotificationType.DCA_EXECUTE:
                self.stats["total_dca"] += 1
            elif notif.type == NotificationType.TAKE_PROFIT:
                self.stats["total_take_profit"] += 1
            elif notif.type == NotificationType.STOP_LOSS:
                self.stats["total_stop_loss"] += 1
        
        return {
            **self.stats,
            "running": self._running,
            "positions": len(self.strategy.positions),
            "strategy": self.strategy.get_status()
        }
    
    def get_status(self) -> Dict[str, Any]:
        """Get current bot status"""
        positions_summary = []
        for pair, pos in self.strategy.positions.items():
            positions_summary.append({
                "pair": pair,
                "bingx_side": pos.bingx_side,
                "size": pos.base_size,
                "dca_count": pos.dca_count,
                "total_pnl": pos.total_pnl,
                "rsi": pos.current_rsi,
                "is_active": pos.is_active
            })
        
        return {
            "running": self._running,
            "config": {
                "dca_levels": self.config.dca_levels,
                "take_profit_pct": self.config.take_profit_pct,
                "max_dca_times": self.config.max_dca_times,
                "rsi_period": self.config.rsi_period,
                "rsi_oversold": self.config.rsi_oversold,
                "rsi_overbought": self.config.rsi_overbought
            },
            "positions": positions_summary,
            "recent_notifications": [n.to_dict() for n in self.notifications.get_history(10)]
        }
    
    def get_notifications(self, limit: int = 50) -> List[Dict]:
        """Get recent notifications"""
        return [n.to_dict() for n in self.notifications.get_history(limit)]
    
    # === Config Updates ===
    
    def update_config(self, **kwargs):
        """Update configuration"""
        for key, value in kwargs.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)
        
        # Update strategy config
        self.strategy.config = self.config
        
        self.notifications.notify("INFO", "Config updated", kwargs)


# Factory function
def create_scalping_bot(
    bingx_client,
    binance_client,
    **config_overrides
) -> ScalpingBot:
    """
    Create a ScalpingBot with custom configuration
    
    Args:
        bingx_client: BingX client
        binance_client: Binance client
        **config_overrides: Override default config values
            - dca_levels: List[float] = [-0.3, -0.6, -1.0, -1.5, -2.0]
            - dca_multiplier: float = 1.0
            - max_dca_times: int = 3
            - take_profit_pct: float = 0.5
            - rsi_period: int = 14
            - rsi_oversold: int = 30
            - rsi_overbought: int = 70
            - max_drawdown_pct: float = 5.0
    
    Returns:
        ScalpingBot instance
    """
    config = ScalpingConfig(**config_overrides)
    return ScalpingBot(bingx_client, binance_client, config)

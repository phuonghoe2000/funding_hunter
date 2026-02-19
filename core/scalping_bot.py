"""
Scalping Bot Controller

Bot controller that manages the scalping strategy with notifications.
Operates in notification-only mode - sends signals but doesn't auto-execute trades.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass, field
from enum import Enum
import threading

from .scalping_strategy import (
    ScalpingStrategy,
    ScalpingConfig,
    ScalpingPosition,
    TradingMode,
    create_scalping_strategy
)

logger = logging.getLogger(__name__)


class NotificationType(Enum):
    """Types of notifications"""
    INFO = "info"
    WARNING = "warning"
    SIGNAL = "signal"  # Trading signal - requires action
    DCA = "dca"  # DCA order signal
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    ERROR = "error"


@dataclass
class Notification:
    """Notification record"""
    timestamp: datetime
    type: NotificationType
    title: str
    message: str
    data: Dict[str, Any] = field(default_factory=dict)
    is_read: bool = False
    requires_action: bool = False
    
    def to_dict(self) -> Dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "type": self.type.value,
            "title": self.title,
            "message": self.message,
            "data": self.data,
            "is_read": self.is_read,
            "requires_action": self.requires_action
        }


class NotificationManager:
    """
    Manages notifications for the scalping bot.
    
    Keeps a history of all notifications and provides callbacks
    for GUI integration.
    """
    
    def __init__(self, max_history: int = 100):
        self.notifications: List[Notification] = []
        self.max_history = max_history
        self._callbacks: List[Callable[[Notification], None]] = []
        self._lock = threading.Lock()
    
    def add_callback(self, callback: Callable[[Notification], None]):
        """Add a callback to be called when a new notification is received"""
        with self._lock:
            self._callbacks.append(callback)
    
    def remove_callback(self, callback: Callable[[Notification], None]):
        """Remove a notification callback"""
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)
    
    def notify(
        self,
        msg_type: str,
        message: str,
        data: Dict[str, Any] = None
    ):
        """Create and dispatch a notification"""
        # Map message types to NotificationType
        type_map = {
            "POSITION_OPENED": NotificationType.INFO,
            "DCA_SIGNAL": NotificationType.SIGNAL,
            "DCA_EXECUTE": NotificationType.DCA,
            "TAKE_PROFIT": NotificationType.TAKE_PROFIT,
            "STOP_LOSS": NotificationType.STOP_LOSS,
            "ERROR": NotificationType.ERROR,
            "INFO": NotificationType.INFO,
            "WARNING": NotificationType.WARNING,
        }
        
        notif_type = type_map.get(msg_type, NotificationType.INFO)
        requires_action = msg_type in ["DCA_EXECUTE", "TAKE_PROFIT", "STOP_LOSS"]
        
        notification = Notification(
            timestamp=datetime.now(timezone.utc),
            type=notif_type,
            title=msg_type.replace("_", " ").title(),
            message=message,
            data=data or {},
            requires_action=requires_action
        )
        
        with self._lock:
            self.notifications.append(notification)
            
            # Trim history
            if len(self.notifications) > self.max_history:
                self.notifications = self.notifications[-self.max_history:]
            
            # Call callbacks
            for callback in self._callbacks:
                try:
                    callback(notification)
                except Exception as e:
                    logger.error(f"Error in notification callback: {e}")
        
        # Log based on type
        if notif_type == NotificationType.ERROR:
            logger.error(f"[{msg_type}] {message}")
        elif notif_type in [NotificationType.DCA, NotificationType.TAKE_PROFIT, NotificationType.STOP_LOSS]:
            logger.warning(f"[{msg_type}] {message}")
        else:
            logger.info(f"[{msg_type}] {message}")
        
        return notification
    
    def get_unread(self) -> List[Notification]:
        """Get all unread notifications"""
        with self._lock:
            return [n for n in self.notifications if not n.is_read]
    
    def get_action_required(self) -> List[Notification]:
        """Get notifications requiring action"""
        with self._lock:
            return [n for n in self.notifications if n.requires_action and not n.is_read]
    
    def mark_read(self, notification: Notification):
        """Mark a notification as read"""
        notification.is_read = True
    
    def mark_all_read(self):
        """Mark all notifications as read"""
        with self._lock:
            for n in self.notifications:
                n.is_read = True
    
    def get_history(self, limit: int = 50) -> List[Notification]:
        """Get notification history"""
        with self._lock:
            return self.notifications[-limit:]
    
    def clear_history(self):
        """Clear all notifications"""
        with self._lock:
            self.notifications.clear()


class ScalpingBot:
    """
    Scalping Bot Controller
    
    Coordinates the scalping strategy with GUI integration.
    Operates in notification-only mode.
    """
    
    def __init__(
        self,
        bingx_client,
        binance_client,
        config: Optional[ScalpingConfig] = None
    ):
        """
        Initialize the scalping bot.
        
        Args:
            bingx_client: BingX exchange client
            binance_client: Binance exchange client
            config: Scalping configuration (optional, uses defaults if not provided)
        """
        self.bingx = bingx_client
        self.binance = binance_client
        self.config = config or ScalpingConfig()
        
        # Notification manager
        self.notifications = NotificationManager()
        
        # Create strategy
        self.strategy = ScalpingStrategy(
            config=self.config,
            bingx_client=bingx_client,
            binance_client=binance_client,
            on_notification=self.notifications.notify
        )
        
        # State
        self._running = False
        self._mode = TradingMode.SCALPING
    
    @property
    def is_running(self) -> bool:
        return self._running
    
    @property
    def mode(self) -> TradingMode:
        return self._mode
    
    def set_mode(self, mode: TradingMode):
        """Set trading mode"""
        self._mode = mode
        self.notifications.notify(
            "INFO",
            f"Trading mode changed to {mode.value}",
            {"mode": mode.value}
        )
    
    async def start(self):
        """Start the scalping bot"""
        if self._running:
            return
        
        self._running = True
        await self.strategy.start()
        self.notifications.notify(
            "INFO",
            "Scalping bot started",
            {"config": self.config.__dict__}
        )
    
    async def stop(self):
        """Stop the scalping bot"""
        if not self._running:
            return
        
        self._running = False
        await self.strategy.stop()
        self.notifications.notify(
            "INFO",
            "Scalping bot stopped",
            {}
        )
    
    def start_sync(self):
        """Start bot synchronously (for GUI integration)"""
        asyncio.create_task(self.start())
    
    def stop_sync(self):
        """Stop bot synchronously (for GUI integration)"""
        asyncio.create_task(self.stop())
    
    async def open_position(
        self,
        pair: str,
        size: float,
        leverage: int,
        bingx_side: str = "SHORT"
    ) -> Optional[ScalpingPosition]:
        """
        Open a new scalping position.
        
        In notification mode, this registers the position for monitoring
        and sends notifications for DCA signals.
        
        Args:
            pair: Trading pair (e.g., "BTC/USDT")
            size: Base position size in USDT
            leverage: Leverage to use
            bingx_side: Side for BingX ("LONG" or "SHORT")
        
        Returns:
            ScalpingPosition if successful
        """
        return await self.strategy.open_position(pair, size, leverage, bingx_side)
    
    def close_position(self, pair: str):
        """Remove position from tracking"""
        self.strategy.remove_position(pair)
        self.notifications.notify(
            "INFO",
            f"Position {pair} removed from tracking",
            {"pair": pair}
        )
    
    def get_position(self, pair: str) -> Optional[ScalpingPosition]:
        """Get position by pair"""
        return self.strategy.get_position(pair)
    
    def get_all_positions(self) -> Dict[str, ScalpingPosition]:
        """Get all tracked positions"""
        return self.strategy.get_all_positions()
    
    def get_status(self) -> Dict[str, Any]:
        """Get bot status"""
        strategy_status = self.strategy.get_status()
        
        return {
            "running": self._running,
            "mode": self._mode.value,
            "unread_notifications": len(self.notifications.get_unread()),
            "action_required": len(self.notifications.get_action_required()),
            **strategy_status
        }
    
    def update_config(self, **kwargs):
        """Update configuration values"""
        for key, value in kwargs.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)
        
        self.notifications.notify(
            "INFO",
            "Configuration updated",
            {"updates": kwargs}
        )
    
    def add_notification_callback(self, callback: Callable[[Notification], None]):
        """Add callback for new notifications (for GUI integration)"""
        self.notifications.add_callback(callback)
    
    def remove_notification_callback(self, callback: Callable[[Notification], None]):
        """Remove notification callback"""
        self.notifications.remove_callback(callback)
    
    def get_pending_actions(self) -> List[Dict]:
        """Get list of pending actions (for GUI display)"""
        actions = []
        
        for notif in self.notifications.get_action_required():
            action = {
                "timestamp": notif.timestamp.isoformat(),
                "type": notif.type.value,
                "message": notif.message,
                "pair": notif.data.get("pair", ""),
                "action": notif.data.get("action", ""),
            }
            
            # Add exchange-specific order info if available
            if "bingx_order" in notif.data:
                action["bingx_order"] = notif.data["bingx_order"]
            if "binance_order" in notif.data:
                action["binance_order"] = notif.data["binance_order"]
            
            actions.append(action)
        
        return actions


# Factory function for easier initialization
def create_scalping_bot(
    bingx_client,
    binance_client,
    **config_kwargs
) -> ScalpingBot:
    """
    Create a ScalpingBot with custom configuration.
    
    Args:
        bingx_client: BingX exchange client
        binance_client: Binance exchange client
        **config_kwargs: Configuration overrides
    
    Returns:
        ScalpingBot instance
    """
    config = ScalpingConfig(**config_kwargs) if config_kwargs else None
    return ScalpingBot(bingx_client, binance_client, config)

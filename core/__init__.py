"""
Core module
"""
from .position_manager import PositionManager, ArbitragePosition, ArbitrageStatus
from .funding_monitor import FundingMonitor, FundingOpportunity
from .scalping_strategy import (
    TradingMode,
    ScalpingConfig,
    ScalpingStrategy,
    ScalpingPosition,
    DCAOrder,
    RSICalculator,
    create_scalping_strategy
)
from .scalping_bot import (
    ScalpingBot,
    NotificationManager,
    Notification,
    NotificationType,
    create_scalping_bot
)

__all__ = [
    # Position management
    "PositionManager", "ArbitragePosition", "ArbitrageStatus",
    "FundingMonitor", "FundingOpportunity",
    # Scalping
    "TradingMode", "ScalpingConfig", "ScalpingStrategy", "ScalpingPosition",
    "DCAOrder", "RSICalculator", "create_scalping_strategy",
    # Scalping bot
    "ScalpingBot", "NotificationManager", "Notification", "NotificationType",
    "create_scalping_bot"
]

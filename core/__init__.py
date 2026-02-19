"""
Core module
"""
from .position_manager import PositionManager, ArbitragePosition, ArbitrageStatus
from .funding_monitor import FundingMonitor, FundingOpportunity
from .scalping_strategy import (
    ScalpingStrategy, 
    ScalpingConfig, 
    ScalpingPosition,
    RSICalculator,
    DCAOrder,
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
    # Legacy
    "PositionManager", "ArbitragePosition", "ArbitrageStatus",
    "FundingMonitor", "FundingOpportunity",
    # Scalping
    "ScalpingStrategy", "ScalpingConfig", "ScalpingPosition",
    "RSICalculator", "DCAOrder", "create_scalping_strategy",
    "ScalpingBot", "NotificationManager", "Notification", 
    "NotificationType", "create_scalping_bot"
]

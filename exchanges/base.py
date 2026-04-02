"""
Base exchange client interface
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
from datetime import datetime

from config.constants import Side, OrderType, PositionStatus


@dataclass
class Position:
    """Position data"""
    symbol: str
    side: Side
    size: float
    entry_price: float
    mark_price: float
    liquidation_price: float
    unrealized_pnl: float
    leverage: int
    status: PositionStatus
    timestamp: datetime
    raw_data: Optional[Dict[str, Any]] = None


@dataclass
class Order:
    """Order data"""
    order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    size: float
    price: Optional[float]
    filled_size: float
    avg_price: float
    status: str
    timestamp: datetime
    raw_data: Optional[Dict[str, Any]] = None


@dataclass
class FundingRate:
    """Funding rate data"""
    symbol: str
    funding_rate: float
    next_funding_time: datetime
    estimated_rate: Optional[float] = None
    funding_interval_hours: int = 8  # Default 8 hours, can be 4 or other values
    raw_data: Optional[Dict[str, Any]] = None


@dataclass
class Balance:
    """Account balance data"""
    currency: str
    total: float
    available: float
    frozen: float
    raw_data: Optional[Dict[str, Any]] = None


class BaseExchangeClient(ABC):
    """Abstract base class for exchange clients"""
    
    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key
        self._session = None
    
    @abstractmethod
    async def connect(self) -> bool:
        """Connect to exchange API"""
        pass
    
    @abstractmethod
    async def disconnect(self):
        """Disconnect from exchange API"""
        pass
    
    @abstractmethod
    async def get_balance(self, currency: str = "USDT") -> Balance:
        """Get account balance"""
        pass
    
    @abstractmethod
    async def get_position(self, symbol: str) -> Optional[Position]:
        """Get position for symbol"""
        pass
    
    @abstractmethod
    async def get_all_positions(self) -> List[Position]:
        """Get all open positions"""
        pass
    
    @abstractmethod
    async def place_market_order(
        self,
        symbol: str,
        side: Side,
        size: float,
        reduce_only: bool = False
    ) -> Order:
        """Place a market order"""
        pass
    
    @abstractmethod
    async def close_position(self, symbol: str, aggressive: bool = False) -> Order:
        """Close position for symbol
        
        Args:
            symbol: Trading symbol
            aggressive: If True, uses aggressive limit order for guaranteed fast fill
        """
        pass
    
    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for symbol"""
        pass
    
    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """Get current funding rate"""
        pass
    
    @abstractmethod
    async def get_mark_price(self, symbol: str) -> float:
        """Get current mark price"""
        pass
    
    @abstractmethod
    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """Get ticker data"""
        pass

    @abstractmethod
    async def get_order_book(self, symbol: str, limit: int = 20) -> Dict[str, Any]:
        """Get current order book snapshot"""
        pass
    
    @abstractmethod
    def get_exchange_name(self) -> str:
        """Get exchange name"""
        pass
    
    @abstractmethod
    async def get_closed_pnl(self, symbol: str, since: Optional[int] = None) -> Dict[str, Any]:
        """
        Get realized PnL, commission fees, and funding fees for a closed position.
        
        Args:
            symbol: Trading symbol
            since: Timestamp in milliseconds of when the position was opened
            
        Returns:
            Dictionary containing:
            - realized_pnl: Profit/Loss from price difference
            - commission: Trading fees paid
            - funding_fee: Funding fees paid/received (positive = received, negative = paid)
            - net_pnl: realized_pnl - commission + funding_fee
        """
        pass

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
    raw_data: Dict[str, Any] = None


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
    raw_data: Dict[str, Any] = None


@dataclass
class FundingRate:
    """Funding rate data"""
    symbol: str
    funding_rate: float
    next_funding_time: datetime
    estimated_rate: Optional[float] = None
    raw_data: Dict[str, Any] = None


@dataclass
class Balance:
    """Account balance data"""
    currency: str
    total: float
    available: float
    frozen: float
    raw_data: Dict[str, Any] = None


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
    async def close_position(self, symbol: str) -> Order:
        """Close position for symbol"""
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
    def get_exchange_name(self) -> str:
        """Get exchange name"""
        pass

"""
Base interfaces and data models for spot exchange clients.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional


@dataclass
class SpotBalance:
    asset: str
    total: float
    available: float
    frozen: float
    raw_data: Optional[dict[str, Any]] = None


@dataclass
class SpotOrder:
    order_id: str
    symbol: str
    side: str
    size: float
    filled_size: float
    avg_price: float
    status: str
    timestamp: datetime
    raw_data: Optional[dict[str, Any]] = None


class BaseSpotExchangeClient(ABC):
    """Abstract base class for spot execution clients."""

    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key
        self._session = None

    @abstractmethod
    async def connect(self) -> bool:
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        pass

    @abstractmethod
    async def get_balance(self, asset: str = "USDT") -> SpotBalance:
        pass

    @abstractmethod
    async def get_order_book(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        pass

    @abstractmethod
    async def get_ticker(self, symbol: str) -> dict[str, Any]:
        pass

    @abstractmethod
    async def get_mark_price(self, symbol: str) -> float:
        pass

    @abstractmethod
    async def place_market_order(self, symbol: str, side: str, quantity: float) -> SpotOrder:
        pass

    @abstractmethod
    async def get_symbol_info(self, symbol: str) -> dict[str, Any]:
        pass

    @abstractmethod
    def get_exchange_name(self) -> str:
        pass

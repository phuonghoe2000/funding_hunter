"""
Dry Run Exchange Wrapper

Wraps a real exchange client and simulates order execution without placing real trades.
Perfect for testing strategies without risking real money.
"""

import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
import uuid

from config.constants import Side, OrderType, PositionStatus
from exchanges.base import BaseExchangeClient, Position, Order, FundingRate, Balance

logger = logging.getLogger(__name__)


class DryRunExchangeWrapper:
    """
    Wraps an exchange client and simulates trades.
    
    All read operations (get_balance, get_ticker, get_mark_price) are passed through
    to the real exchange. Write operations (place_market_order, close_position) are
    simulated locally.
    """
    
    def __init__(self, real_client: BaseExchangeClient):
        """
        Initialize with a real exchange client.
        
        Args:
            real_client: The actual exchange client to wrap
        """
        self.real_client = real_client
        
        # Simulated state
        self._simulated_positions: Dict[str, Position] = {}
        self._simulated_orders: List[Order] = []
        self._simulated_balance: float = 10000.0  # Start with 10k USDT
        self._order_counter: int = 0
        
        logger.info("DryRunExchangeWrapper initialized - trades will be simulated")
    
    # ==================== Pass-through methods ====================
    
    async def connect(self) -> bool:
        """Pass through to real client"""
        return await self.real_client.connect()
    
    async def disconnect(self):
        """Pass through to real client"""
        await self.real_client.disconnect()
    
    async def get_balance(self, currency: str = "USDT") -> Balance:
        """Return simulated balance"""
        return Balance(
            currency=currency,
            total=self._simulated_balance,
            available=self._simulated_balance,
            frozen=0.0
        )
    
    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """Pass through to real client"""
        return await self.real_client.get_funding_rate(symbol)
    
    async def get_mark_price(self, symbol: str) -> float:
        """Pass through to real client"""
        return await self.real_client.get_mark_price(symbol)
    
    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """Pass through to real client"""
        return await self.real_client.get_ticker(symbol)
    
    async def get_klines(self, symbol: str, interval: str = "1m", limit: int = 100) -> List[Dict]:
        """Pass through to real client for volume data"""
        return await self.real_client.get_klines(symbol, interval, limit)
    
    def get_exchange_name(self) -> str:
        """Return wrapped exchange name with dry run indicator"""
        return f"{self.real_client.get_exchange_name()} (DRY RUN)"
    
    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Simulate setting leverage (always succeeds)"""
        logger.info(f"[DRY RUN] Set leverage for {symbol} to {leverage}x")
        return True
    
    # ==================== Simulated trading methods ====================
    
    async def get_position(self, symbol: str) -> Optional[Position]:
        """Get simulated position"""
        return self._simulated_positions.get(symbol)
    
    async def get_all_positions(self) -> List[Position]:
        """Get all simulated positions"""
        return list(self._simulated_positions.values())
    
    async def place_market_order(
        self,
        symbol: str,
        side: Side,
        size: float,
        reduce_only: bool = False,
        position_side: str = None
    ) -> Order:
        """
        Simulate placing a market order.
        
        Args:
            symbol: Trading symbol
            side: LONG or SHORT
            size: Order size in contracts/USDT
            reduce_only: If True, only reduces position
            position_side: For Hedge mode (LONG or SHORT)
            
        Returns:
            Simulated order object
        """
        # Get current market price
        try:
            current_price = await self.real_client.get_mark_price(symbol)
        except Exception:
            # Fallback to a dummy price if real price unavailable
            current_price = 50000.0 if "BTC" in symbol else 3000.0
        
        # Generate order ID
        self._order_counter += 1
        order_id = f"DRY_RUN_{self._order_counter}_{uuid.uuid4().hex[:8]}"
        
        # Create order object
        order = Order(
            order_id=order_id,
            symbol=symbol,
            side=side,
            order_type=OrderType.MARKET,
            size=size,
            price=None,
            filled_size=size,
            avg_price=current_price,
            status="FILLED",
            timestamp=datetime.now(timezone.utc)
        )
        
        self._simulated_orders.append(order)
        
        # Update simulated position
        self._update_simulated_position(symbol, side, size, current_price, reduce_only)
        
        logger.info(
            f"[DRY RUN] Market order {side.value} {size} {symbol} @ ${current_price:,.2f} "
            f"| Order ID: {order_id}"
        )
        
        return order
    
    def _update_simulated_position(
        self,
        symbol: str,
        side: Side,
        size: float,
        price: float,
        reduce_only: bool
    ):
        """Update simulated position based on order"""
        existing = self._simulated_positions.get(symbol)
        
        if existing:
            # Close or modify existing position
            if reduce_only or (existing.side == Side.LONG and side == Side.SHORT) or \
               (existing.side == Side.SHORT and side == Side.LONG):
                # Closing position
                if size >= existing.size:
                    # Fully closed
                    pnl = self._calculate_pnl(existing, price)
                    self._simulated_balance += pnl
                    del self._simulated_positions[symbol]
                    logger.info(f"[DRY RUN] Position closed | PnL: ${pnl:,.2f}")
                else:
                    # Partially closed
                    existing.size -= size
                    existing.mark_price = price
            else:
                # Adding to position
                total_cost = (existing.entry_price * existing.size) + (price * size)
                existing.size += size
                existing.entry_price = total_cost / existing.size
                existing.mark_price = price
        else:
            # Create new position
            pos_side = Side.LONG if side == Side.LONG else Side.SHORT
            self._simulated_positions[symbol] = Position(
                symbol=symbol,
                side=pos_side,
                size=size,
                entry_price=price,
                mark_price=price,
                liquidation_price=0.0,  # Not calculated in dry run
                unrealized_pnl=0.0,
                leverage=10,  # Default
                status=PositionStatus.OPEN,
                timestamp=datetime.now(timezone.utc)
            )
            logger.info(f"[DRY RUN] Position opened: {pos_side.value} {size} @ ${price:,.2f}")
    
    def _calculate_pnl(self, position: Position, exit_price: float) -> float:
        """Calculate PnL for closing a position"""
        if position.side == Side.LONG:
            return (exit_price - position.entry_price) * position.size
        else:
            return (position.entry_price - exit_price) * position.size
    
    async def place_stop_market_order(
        self,
        symbol: str,
        side: Side,
        size: float,
        stop_price: float,
        position_side: str = None
    ) -> Order:
        """Simulate placing a STOP_MARKET order (Stop Loss)"""
        from config.constants import OrderType
        
        self._order_counter += 1
        order_id = f"DRY_SL_{self._order_counter}_{uuid.uuid4().hex[:8]}"
        
        order = Order(
            order_id=order_id,
            symbol=symbol,
            side=side,
            order_type=OrderType.STOP_MARKET,
            size=size,
            price=stop_price,
            filled_size=0,
            avg_price=0,
            status="NEW",
            timestamp=datetime.now(timezone.utc)
        )
        
        self._simulated_orders.append(order)
        logger.info(f"[DRY RUN] STOP_MARKET order: {side.value} {size} {symbol} @ stop {stop_price:.2f}")
        
        return order
    
    async def place_take_profit_market_order(
        self,
        symbol: str,
        side: Side,
        size: float,
        stop_price: float,
        position_side: str = None
    ) -> Order:
        """Simulate placing a TAKE_PROFIT_MARKET order"""
        from config.constants import OrderType
        
        self._order_counter += 1
        order_id = f"DRY_TP_{self._order_counter}_{uuid.uuid4().hex[:8]}"
        
        order = Order(
            order_id=order_id,
            symbol=symbol,
            side=side,
            order_type=OrderType.TAKE_PROFIT_MARKET,
            size=size,
            price=stop_price,
            filled_size=0,
            avg_price=0,
            status="NEW",
            timestamp=datetime.now(timezone.utc)
        )
        
        self._simulated_orders.append(order)
        logger.info(f"[DRY RUN] TAKE_PROFIT_MARKET order: {side.value} {size} {symbol} @ tp {stop_price:.2f}")
        
        return order
    
    async def cancel_order(self, symbol: str, order_id: str, is_algo: bool = True) -> bool:
        """Simulate cancelling an order"""
        for order in self._simulated_orders:
            if order.order_id == order_id:
                order.status = "CANCELLED"
                logger.info(f"[DRY RUN] Order {order_id} cancelled")
                return True
        logger.warning(f"[DRY RUN] Order {order_id} not found")
        return False
    
    async def cancel_all_orders(self, symbol: str) -> bool:
        """Simulate cancelling all orders for a symbol"""
        count = 0
        for order in self._simulated_orders:
            if order.symbol == symbol and order.status == "NEW":
                order.status = "CANCELLED"
                count += 1
        logger.info(f"[DRY RUN] Cancelled {count} orders for {symbol}")
        return True
    
    async def close_position(self, symbol: str, aggressive: bool = False) -> Optional[Order]:
        """
        Simulate closing a position.
        
        Args:
            symbol: Trading symbol
            aggressive: Ignored in dry run
            
        Returns:
            Simulated order object or None if no position
        """
        position = self._simulated_positions.get(symbol)
        
        if not position:
            logger.warning(f"[DRY RUN] No position to close for {symbol}")
            return None
        
        # Determine close side
        close_side = Side.SHORT if position.side == Side.LONG else Side.LONG
        
        return await self.place_market_order(
            symbol=symbol,
            side=close_side,
            size=position.size,
            reduce_only=True
        )
    
    # ==================== Stats and info ====================
    
    def get_stats(self) -> Dict[str, Any]:
        """Get dry run statistics"""
        return {
            "mode": "DRY RUN",
            "balance": self._simulated_balance,
            "total_orders": len(self._simulated_orders),
            "open_positions": len(self._simulated_positions),
            "positions": {
                sym: {
                    "side": pos.side.value,
                    "size": pos.size,
                    "entry": pos.entry_price,
                    "pnl": pos.unrealized_pnl
                }
                for sym, pos in self._simulated_positions.items()
            }
        }
    
    def __getattr__(self, name):
        """
        Forward any unknown attributes to the real client.
        This ensures compatibility with any additional methods.
        """
        return getattr(self.real_client, name)

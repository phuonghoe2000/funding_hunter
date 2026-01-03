"""
Position Manager - Manages positions across both exchanges
Handles opening, closing, and synchronizing positions
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, List, Callable, Any
from enum import Enum
import logging

from config.constants import Side, Exchange, PositionStatus, get_exchange_symbol, get_unified_pair
from config.settings import settings
from exchanges.base import Position, Order
from exchanges.okx_client import OKXClient
from exchanges.binance_client import BinanceClient

logger = logging.getLogger(__name__)


class ArbitrageStatus(Enum):
    """Status of arbitrage position"""
    NONE = "none"
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    ERROR = "error"
    LIQUIDATED = "liquidated"


@dataclass
class ArbitragePosition:
    """Represents a hedged position across two exchanges"""
    pair: str  # Unified pair like "BTC/USDT"
    okx_position: Optional[Position] = None
    binance_position: Optional[Position] = None
    okx_side: Optional[Side] = None
    binance_side: Optional[Side] = None
    size: float = 0.0
    leverage: int = 10
    status: ArbitrageStatus = ArbitrageStatus.NONE
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    total_pnl: float = 0.0
    funding_collected: float = 0.0
    error_message: str = ""


class PositionManager:
    """Manages arbitrage positions across OKX and Binance"""
    
    def __init__(
        self,
        okx_client: OKXClient,
        binance_client: BinanceClient,
        on_position_update: Optional[Callable] = None,
        on_liquidation: Optional[Callable] = None,
        on_error: Optional[Callable] = None
    ):
        self.okx = okx_client
        self.binance = binance_client
        self.on_position_update = on_position_update
        self.on_liquidation = on_liquidation
        self.on_error = on_error
        
        self.active_positions: Dict[str, ArbitragePosition] = {}
        self._monitoring = False
        self._monitor_task: Optional[asyncio.Task] = None
    
    async def initialize(self) -> bool:
        """Initialize position manager and connect to exchanges"""
        try:
            # Connect to both exchanges
            okx_connected = await self.okx.connect()
            binance_connected = await self.binance.connect()
            
            if not okx_connected:
                logger.error("Failed to connect to OKX")
                return False
            
            if not binance_connected:
                logger.error("Failed to connect to Binance")
                return False
            
            # Set hedge mode on both exchanges
            await self.okx.set_position_mode(hedge_mode=True)
            await self.binance.set_position_mode(hedge_mode=True)
            
            logger.info("Position manager initialized successfully")
            return True
            
        except Exception as e:
            logger.error(f"Failed to initialize position manager: {e}")
            return False
    
    async def open_arbitrage_position(
        self,
        pair: str,
        size: float,
        okx_side: Side,
        leverage: int = 10
    ) -> ArbitragePosition:
        """
        Open hedged position on both exchanges
        
        Args:
            pair: Unified trading pair (e.g., "BTC/USDT")
            size: Position size
            okx_side: Side for OKX (Binance will be opposite)
            leverage: Leverage to use
        
        Returns:
            ArbitragePosition object
        """
        arb_position = ArbitragePosition(
            pair=pair,
            okx_side=okx_side,
            binance_side=Side.SHORT if okx_side == Side.LONG else Side.LONG,
            size=size,
            leverage=leverage,
            status=ArbitrageStatus.OPENING
        )
        
        okx_symbol = get_exchange_symbol(pair, Exchange.OKX)
        binance_symbol = get_exchange_symbol(pair, Exchange.BINANCE)
        
        try:
            # Set leverage on both exchanges
            await asyncio.gather(
                self.okx.set_leverage(okx_symbol, leverage),
                self.binance.set_leverage(binance_symbol, leverage)
            )
            
            # Open positions simultaneously
            okx_order, binance_order = await asyncio.gather(
                self.okx.place_market_order(okx_symbol, okx_side, size),
                self.binance.place_market_order(binance_symbol, arb_position.binance_side, size)
            )
            
            # Wait a bit for orders to fill
            await asyncio.sleep(1)
            
            # Get position details
            okx_pos, binance_pos = await asyncio.gather(
                self.okx.get_position(okx_symbol),
                self.binance.get_position(binance_symbol)
            )
            
            arb_position.okx_position = okx_pos
            arb_position.binance_position = binance_pos
            arb_position.status = ArbitrageStatus.OPEN
            arb_position.opened_at = datetime.now(timezone.utc)
            
            # Store in active positions
            self.active_positions[pair] = arb_position
            
            # Notify callback
            if self.on_position_update:
                self.on_position_update(arb_position)
            
            logger.info(f"Opened arbitrage position for {pair}: OKX {okx_side.value}, Binance {arb_position.binance_side.value}")
            
            return arb_position
            
        except Exception as e:
            arb_position.status = ArbitrageStatus.ERROR
            arb_position.error_message = str(e)
            
            # Try to close any partially opened positions
            await self._cleanup_partial_position(pair)
            
            if self.on_error:
                self.on_error(f"Failed to open arbitrage position: {e}")
            
            logger.error(f"Failed to open arbitrage position: {e}")
            raise
    
    async def close_arbitrage_position(self, pair: str) -> ArbitragePosition:
        """Close hedged position on both exchanges"""
        if pair not in self.active_positions:
            raise Exception(f"No active position for {pair}")
        
        arb_position = self.active_positions[pair]
        arb_position.status = ArbitrageStatus.CLOSING
        
        okx_symbol = get_exchange_symbol(pair, Exchange.OKX)
        binance_symbol = get_exchange_symbol(pair, Exchange.BINANCE)
        
        try:
            # Close positions simultaneously
            close_tasks = []
            
            if arb_position.okx_position:
                close_tasks.append(self.okx.close_position(okx_symbol))
            
            if arb_position.binance_position:
                close_tasks.append(self.binance.close_position(binance_symbol))
            
            if close_tasks:
                await asyncio.gather(*close_tasks)
            
            # Calculate final PnL
            okx_pnl = arb_position.okx_position.unrealized_pnl if arb_position.okx_position else 0
            binance_pnl = arb_position.binance_position.unrealized_pnl if arb_position.binance_position else 0
            arb_position.total_pnl = okx_pnl + binance_pnl
            
            arb_position.status = ArbitrageStatus.CLOSED
            arb_position.closed_at = datetime.now(timezone.utc)
            
            # Remove from active positions
            del self.active_positions[pair]
            
            if self.on_position_update:
                self.on_position_update(arb_position)
            
            logger.info(f"Closed arbitrage position for {pair}, PnL: {arb_position.total_pnl}")
            
            return arb_position
            
        except Exception as e:
            arb_position.status = ArbitrageStatus.ERROR
            arb_position.error_message = str(e)
            
            if self.on_error:
                self.on_error(f"Failed to close arbitrage position: {e}")
            
            logger.error(f"Failed to close arbitrage position: {e}")
            raise
    
    async def _cleanup_partial_position(self, pair: str):
        """Clean up partially opened positions"""
        okx_symbol = get_exchange_symbol(pair, Exchange.OKX)
        binance_symbol = get_exchange_symbol(pair, Exchange.BINANCE)
        
        try:
            okx_pos = await self.okx.get_position(okx_symbol)
            if okx_pos:
                await self.okx.close_position(okx_symbol)
        except Exception as e:
            logger.error(f"Failed to cleanup OKX position: {e}")
        
        try:
            binance_pos = await self.binance.get_position(binance_symbol)
            if binance_pos:
                await self.binance.close_position(binance_symbol)
        except Exception as e:
            logger.error(f"Failed to cleanup Binance position: {e}")
    
    async def start_monitoring(self, interval: float = 1.0):
        """Start position monitoring"""
        if self._monitoring:
            return
        
        self._monitoring = True
        self._monitor_task = asyncio.create_task(self._monitor_loop(interval))
        logger.info("Started position monitoring")
    
    async def stop_monitoring(self):
        """Stop position monitoring"""
        self._monitoring = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        logger.info("Stopped position monitoring")
    
    async def _monitor_loop(self, interval: float):
        """Main monitoring loop"""
        while self._monitoring:
            try:
                await self._check_positions()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                await asyncio.sleep(interval)
    
    async def _check_positions(self):
        """Check all active positions for liquidation or changes"""
        for pair, arb_position in list(self.active_positions.items()):
            try:
                okx_symbol = get_exchange_symbol(pair, Exchange.OKX)
                binance_symbol = get_exchange_symbol(pair, Exchange.BINANCE)
                
                # Get current positions
                okx_pos, binance_pos = await asyncio.gather(
                    self.okx.get_position(okx_symbol),
                    self.binance.get_position(binance_symbol)
                )
                
                # Update stored positions
                arb_position.okx_position = okx_pos
                arb_position.binance_position = binance_pos
                
                # Check for liquidation
                okx_liquidated = arb_position.okx_position is None and arb_position.status == ArbitrageStatus.OPEN
                binance_liquidated = arb_position.binance_position is None and arb_position.status == ArbitrageStatus.OPEN
                
                if okx_liquidated or binance_liquidated:
                    await self._handle_liquidation(pair, okx_liquidated, binance_liquidated)
                    continue
                
                # Notify position update
                if self.on_position_update:
                    self.on_position_update(arb_position)
                    
            except Exception as e:
                logger.error(f"Error checking position {pair}: {e}")
    
    async def _handle_liquidation(self, pair: str, okx_liquidated: bool, binance_liquidated: bool):
        """Handle position liquidation on one exchange"""
        arb_position = self.active_positions.get(pair)
        if not arb_position:
            return
        
        liquidated_exchange = "OKX" if okx_liquidated else "Binance"
        logger.warning(f"Position liquidated on {liquidated_exchange} for {pair}")
        
        arb_position.status = ArbitrageStatus.LIQUIDATED
        
        # Auto close the other side if enabled
        if settings.trading.auto_close_on_liquidation:
            try:
                if okx_liquidated and arb_position.binance_position:
                    binance_symbol = get_exchange_symbol(pair, Exchange.BINANCE)
                    await self.binance.close_position(binance_symbol)
                    logger.info(f"Auto-closed Binance position for {pair}")
                    
                elif binance_liquidated and arb_position.okx_position:
                    okx_symbol = get_exchange_symbol(pair, Exchange.OKX)
                    await self.okx.close_position(okx_symbol)
                    logger.info(f"Auto-closed OKX position for {pair}")
                    
            except Exception as e:
                logger.error(f"Failed to auto-close position: {e}")
        
        # Remove from active positions
        del self.active_positions[pair]
        
        # Notify callbacks
        if self.on_liquidation:
            self.on_liquidation(pair, liquidated_exchange)
        
        if self.on_position_update:
            self.on_position_update(arb_position)
    
    async def get_balances(self) -> Dict[str, Any]:
        """Get balances from both exchanges"""
        okx_balance, binance_balance = await asyncio.gather(
            self.okx.get_balance("USDT"),
            self.binance.get_balance("USDT")
        )
        
        return {
            "okx": okx_balance,
            "binance": binance_balance,
            "total": okx_balance.total + binance_balance.total
        }
    
    async def get_funding_rates(self, pair: str) -> Dict[str, Any]:
        """Get funding rates from both exchanges"""
        okx_symbol = get_exchange_symbol(pair, Exchange.OKX)
        binance_symbol = get_exchange_symbol(pair, Exchange.BINANCE)
        
        okx_funding, binance_funding = await asyncio.gather(
            self.okx.get_funding_rate(okx_symbol),
            self.binance.get_funding_rate(binance_symbol)
        )
        
        return {
            "okx": okx_funding,
            "binance": binance_funding,
            "spread": abs(okx_funding.funding_rate - binance_funding.funding_rate)
        }
    
    async def shutdown(self):
        """Shutdown position manager"""
        await self.stop_monitoring()
        await asyncio.gather(
            self.okx.disconnect(),
            self.binance.disconnect()
        )
        logger.info("Position manager shut down")

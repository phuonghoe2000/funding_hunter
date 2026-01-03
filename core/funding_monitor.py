"""
Funding Monitor - Monitors funding rates and calculates arbitrage opportunities
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Callable, Any
import logging

from config.constants import Exchange, POPULAR_PAIRS, get_exchange_symbol
from exchanges.okx_client import OKXClient
from exchanges.binance_client import BinanceClient
from exchanges.base import FundingRate

logger = logging.getLogger(__name__)


@dataclass
class FundingOpportunity:
    """Represents a funding arbitrage opportunity"""
    pair: str
    okx_rate: float
    binance_rate: float
    spread: float
    okx_next_funding: datetime
    binance_next_funding: datetime
    recommended_long: Exchange  # Exchange to go long on
    recommended_short: Exchange  # Exchange to go short on
    estimated_hourly_rate: float  # Estimated hourly funding rate capture


class FundingMonitor:
    """Monitors funding rates across exchanges"""
    
    def __init__(
        self,
        okx_client: OKXClient,
        binance_client: BinanceClient,
        on_funding_update: Optional[Callable] = None,
        on_opportunity: Optional[Callable] = None
    ):
        self.okx = okx_client
        self.binance = binance_client
        self.on_funding_update = on_funding_update
        self.on_opportunity = on_opportunity
        
        self.funding_rates: Dict[str, Dict[str, FundingRate]] = {}
        self.opportunities: List[FundingOpportunity] = []
        
        self._monitoring = False
        self._monitor_task: Optional[asyncio.Task] = None
    
    async def get_funding_rate(self, pair: str) -> Dict[str, FundingRate]:
        """Get funding rates for a pair from both exchanges"""
        okx_symbol = get_exchange_symbol(pair, Exchange.OKX)
        binance_symbol = get_exchange_symbol(pair, Exchange.BINANCE)
        
        try:
            okx_rate, binance_rate = await asyncio.gather(
                self.okx.get_funding_rate(okx_symbol),
                self.binance.get_funding_rate(binance_symbol)
            )
            
            self.funding_rates[pair] = {
                "okx": okx_rate,
                "binance": binance_rate
            }
            
            return self.funding_rates[pair]
            
        except Exception as e:
            logger.error(f"Error getting funding rates for {pair}: {e}")
            raise
    
    async def get_all_funding_rates(self, pairs: List[str] = None) -> Dict[str, Dict[str, FundingRate]]:
        """Get funding rates for all pairs"""
        pairs = pairs or POPULAR_PAIRS
        
        tasks = [self.get_funding_rate(pair) for pair in pairs]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for pair, result in zip(pairs, results):
            if isinstance(result, Exception):
                logger.error(f"Failed to get funding rate for {pair}: {result}")
                continue
        
        return self.funding_rates
    
    def analyze_opportunity(self, pair: str) -> Optional[FundingOpportunity]:
        """Analyze funding arbitrage opportunity for a pair"""
        if pair not in self.funding_rates:
            return None
        
        rates = self.funding_rates[pair]
        okx_rate = rates["okx"]
        binance_rate = rates["binance"]
        
        okx_funding = okx_rate.funding_rate
        binance_funding = binance_rate.funding_rate
        
        spread = okx_funding - binance_funding
        
        # Determine optimal positions
        # If OKX rate > Binance rate: Short OKX (pay less), Long Binance
        # If Binance rate > OKX rate: Short Binance (pay less), Long OKX
        if spread > 0:
            # OKX has higher rate - short OKX, long Binance
            recommended_long = Exchange.BINANCE
            recommended_short = Exchange.OKX
        else:
            # Binance has higher rate - short Binance, long OKX
            recommended_long = Exchange.OKX
            recommended_short = Exchange.BINANCE
        
        # Calculate estimated hourly rate
        # Funding is typically charged every 8 hours
        # We capture the spread by being short on high rate exchange
        estimated_hourly = abs(spread) / 8 * 100  # Convert to percentage per hour
        
        opportunity = FundingOpportunity(
            pair=pair,
            okx_rate=okx_funding * 100,  # Convert to percentage
            binance_rate=binance_funding * 100,
            spread=abs(spread) * 100,
            okx_next_funding=okx_rate.next_funding_time,
            binance_next_funding=binance_rate.next_funding_time,
            recommended_long=recommended_long,
            recommended_short=recommended_short,
            estimated_hourly_rate=estimated_hourly
        )
        
        return opportunity
    
    async def find_best_opportunities(self, min_spread: float = 0.01) -> List[FundingOpportunity]:
        """Find best funding arbitrage opportunities"""
        await self.get_all_funding_rates()
        
        opportunities = []
        for pair in self.funding_rates:
            opp = self.analyze_opportunity(pair)
            if opp and opp.spread >= min_spread:
                opportunities.append(opp)
        
        # Sort by spread (highest first)
        opportunities.sort(key=lambda x: x.spread, reverse=True)
        self.opportunities = opportunities
        
        return opportunities
    
    async def start_monitoring(self, interval: float = 60.0, min_spread: float = 0.01):
        """Start funding rate monitoring"""
        if self._monitoring:
            return
        
        self._monitoring = True
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(interval, min_spread)
        )
        logger.info("Started funding rate monitoring")
    
    async def stop_monitoring(self):
        """Stop funding rate monitoring"""
        self._monitoring = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        logger.info("Stopped funding rate monitoring")
    
    async def _monitor_loop(self, interval: float, min_spread: float):
        """Main monitoring loop"""
        while self._monitoring:
            try:
                opportunities = await self.find_best_opportunities(min_spread)
                
                if self.on_funding_update:
                    self.on_funding_update(self.funding_rates)
                
                if self.on_opportunity and opportunities:
                    self.on_opportunity(opportunities)
                
                await asyncio.sleep(interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in funding monitor loop: {e}")
                await asyncio.sleep(interval)
    
    def get_time_to_funding(self, pair: str) -> Dict[str, timedelta]:
        """Get time until next funding for a pair"""
        if pair not in self.funding_rates:
            return {}
        
        now = datetime.now(timezone.utc)
        rates = self.funding_rates[pair]
        
        return {
            "okx": rates["okx"].next_funding_time - now,
            "binance": rates["binance"].next_funding_time - now
        }
    
    def format_opportunity(self, opp: FundingOpportunity) -> str:
        """Format opportunity for display"""
        return (
            f"{opp.pair}:\n"
            f"  OKX Rate: {opp.okx_rate:.4f}%\n"
            f"  Binance Rate: {opp.binance_rate:.4f}%\n"
            f"  Spread: {opp.spread:.4f}%\n"
            f"  Recommendation: Long {opp.recommended_long.value}, Short {opp.recommended_short.value}\n"
            f"  Est. Hourly Rate: {opp.estimated_hourly_rate:.4f}%"
        )

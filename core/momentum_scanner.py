"""
Momentum Scanner

Scans all trading pairs to find momentum signals.
Runs periodically (e.g., every 15 minutes) to identify opportunities.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Callable, Any
from enum import Enum

logger = logging.getLogger(__name__)


class SignalType(Enum):
    """Signal type for display"""
    STRONG_LONG = "🟢🟢 STRONG LONG"
    LONG = "🟢 LONG"
    WEAK_LONG = "🟡 WEAK LONG"
    NEUTRAL = "⚪ NEUTRAL"
    WEAK_SHORT = "🟡 WEAK SHORT"
    SHORT = "🔴 SHORT"
    STRONG_SHORT = "🔴🔴 STRONG SHORT"


@dataclass
class ScanResult:
    """Result of scanning a single pair"""
    symbol: str
    price: float
    price_change_pct: float  # Price change over timeframe
    volume_24h: float
    signal_type: SignalType
    score: float  # Absolute score for sorting (higher = stronger signal)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "price": self.price,
            "change_pct": self.price_change_pct,
            "volume_24h": self.volume_24h,
            "signal": self.signal_type.value,
            "score": self.score,
            "time": self.timestamp.isoformat()
        }


@dataclass
class ScanConfig:
    """Scanner configuration"""
    scan_interval_seconds: int = 900  # 15 minutes
    min_volume_24h: float = 1_000_000  # Minimum 24h volume in USDT
    price_change_threshold: float = 1.0  # % change to trigger signal
    strong_threshold_multiplier: float = 2.0  # 2x threshold = strong signal
    max_concurrent_requests: int = 10  # Limit concurrent API calls
    timeout_per_symbol: float = 5.0  # Timeout for each symbol


class MomentumScanner:
    """
    Scans all trading pairs for momentum signals.
    
    Usage:
        scanner = MomentumScanner(exchange_client, config)
        results = await scanner.scan_all()
        # or
        await scanner.start_periodic_scan(callback)
    """
    
    def __init__(
        self,
        exchange_client,
        config: Optional[ScanConfig] = None,
        on_scan_complete: Optional[Callable[[List[ScanResult]], None]] = None,
        on_log: Optional[Callable[[str], None]] = None
    ):
        self.exchange = exchange_client
        self.config = config or ScanConfig()
        self.on_scan_complete = on_scan_complete
        self.log = on_log or (lambda m: logger.info(m))
        
        # State
        self._running = False
        self._scan_task: Optional[asyncio.Task] = None
        self._last_scan_time: Optional[datetime] = None
        self._last_results: List[ScanResult] = []
        
        # Price history for calculating change (symbol -> list of (price, time))
        self._price_history: Dict[str, List[tuple]] = {}
    
    async def get_symbols(self) -> List[str]:
        """Get all tradeable symbols"""
        try:
            symbols = await self.exchange.get_all_symbols()
            return symbols
        except Exception as e:
            self.log(f"Error getting symbols: {e}")
            return []
    
    async def scan_symbol(self, symbol: str) -> Optional[ScanResult]:
        """Scan a single symbol for momentum"""
        try:
            # Get ticker data
            ticker = await self.exchange.get_ticker(symbol)
            
            price = float(ticker.get("last", 0))
            volume_24h = float(ticker.get("volume", 0)) * price  # Convert to USDT
            
            if price <= 0:
                logger.debug(f"[{symbol}] Skipped: price <= 0")
                return None
            
            # Filter by minimum volume
            if volume_24h < self.config.min_volume_24h:
                logger.debug(f"[{symbol}] Skipped: volume ${volume_24h:,.0f} < min ${self.config.min_volume_24h:,.0f}")
                return None
            
            # Calculate price change
            price_change_pct = await self._calculate_price_change(symbol, price)
            
            # Determine signal type and score
            signal_type, score = self._classify_signal(price_change_pct)
            
            return ScanResult(
                symbol=symbol,
                price=price,
                price_change_pct=price_change_pct,
                volume_24h=volume_24h,
                signal_type=signal_type,
                score=score
            )
            
        except Exception as e:
            logger.debug(f"Error scanning {symbol}: {e}")
            return None
    
    async def _calculate_price_change(self, symbol: str, current_price: float) -> float:
        """Calculate price change over the scan interval"""
        now = datetime.now(timezone.utc)
        
        # Initialize history for this symbol
        if symbol not in self._price_history:
            self._price_history[symbol] = []
        
        history = self._price_history[symbol]
        
        # Add current price
        history.append((current_price, now))
        
        # Remove old entries (keep only last 2 hours of data)
        cutoff = now.timestamp() - 7200
        history[:] = [(p, t) for p, t in history if t.timestamp() > cutoff]
        
        # Find price from scan_interval_seconds ago
        target_time = now.timestamp() - self.config.scan_interval_seconds
        
        old_price = None
        for price, timestamp in history:
            if timestamp.timestamp() <= target_time:
                old_price = price
                break
        
        if old_price is None and len(history) > 1:
            # Use oldest available price
            old_price = history[0][0]
        
        if old_price and old_price > 0:
            return ((current_price - old_price) / old_price) * 100
        
        return 0.0
    
    def _classify_signal(self, price_change_pct: float) -> tuple:
        """Classify signal type based on price change"""
        threshold = self.config.price_change_threshold
        strong_threshold = threshold * self.config.strong_threshold_multiplier
        
        abs_change = abs(price_change_pct)
        score = abs_change  # Score is absolute change for sorting
        
        if price_change_pct >= strong_threshold:
            return SignalType.STRONG_LONG, score
        elif price_change_pct >= threshold:
            return SignalType.LONG, score
        elif price_change_pct >= threshold * 0.5:
            return SignalType.WEAK_LONG, score
        elif price_change_pct <= -strong_threshold:
            return SignalType.STRONG_SHORT, score
        elif price_change_pct <= -threshold:
            return SignalType.SHORT, score
        elif price_change_pct <= -threshold * 0.5:
            return SignalType.WEAK_SHORT, score
        else:
            return SignalType.NEUTRAL, score
    
    async def scan_all(self, symbols: Optional[List[str]] = None) -> List[ScanResult]:
        """
        Scan all symbols and return sorted results.
        
        Args:
            symbols: Optional list of symbols to scan. If None, fetches all from exchange.
            
        Returns:
            List of ScanResult sorted by score (strongest signals first)
        """
        if symbols is None:
            symbols = await self.get_symbols()
        
        if not symbols:
            self.log("❌ No symbols to scan - check exchange connection")
            return []
        
        self.log(f"🔍 Scanning {len(symbols)} pairs...")
        start_time = datetime.now()
        
        results: List[ScanResult] = []
        skipped_volume = 0
        skipped_error = 0
        skipped_timeout = 0
        
        # Use semaphore to limit concurrent requests
        semaphore = asyncio.Semaphore(self.config.max_concurrent_requests)
        
        async def scan_with_limit(symbol: str) -> Optional[ScanResult]:
            nonlocal skipped_timeout
            async with semaphore:
                try:
                    return await asyncio.wait_for(
                        self.scan_symbol(symbol),
                        timeout=self.config.timeout_per_symbol
                    )
                except asyncio.TimeoutError:
                    skipped_timeout += 1
                    logger.debug(f"Timeout scanning {symbol}")
                    return None
        
        # Scan all symbols concurrently
        tasks = [scan_with_limit(s) for s in symbols]
        scan_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for result in scan_results:
            if isinstance(result, ScanResult):
                results.append(result)
            elif isinstance(result, Exception):
                skipped_error += 1
        
        # Count by signal type
        strong_long = len([r for r in results if r.signal_type == SignalType.STRONG_LONG])
        long_signals = len([r for r in results if r.signal_type == SignalType.LONG])
        weak_long = len([r for r in results if r.signal_type == SignalType.WEAK_LONG])
        neutral = len([r for r in results if r.signal_type == SignalType.NEUTRAL])
        weak_short = len([r for r in results if r.signal_type == SignalType.WEAK_SHORT])
        short_signals = len([r for r in results if r.signal_type == SignalType.SHORT])
        strong_short = len([r for r in results if r.signal_type == SignalType.STRONG_SHORT])
        
        # Sort by score (strongest signals first)
        results.sort(key=lambda r: r.score, reverse=True)
        
        # Filter out neutral signals for cleaner output
        significant_results = [r for r in results if r.signal_type != SignalType.NEUTRAL]
        
        elapsed = (datetime.now() - start_time).total_seconds()
        
        # Detailed log
        self.log(f"📊 Scan complete in {elapsed:.1f}s:")
        self.log(f"   Total scanned: {len(symbols)} | Got data: {len(results)} | Timeout: {skipped_timeout}")
        self.log(f"   🟢🟢 Strong Long: {strong_long} | 🟢 Long: {long_signals} | 🟡 Weak Long: {weak_long}")
        self.log(f"   🔴🔴 Strong Short: {strong_short} | 🔴 Short: {short_signals} | 🟡 Weak Short: {weak_short}")
        self.log(f"   ⚪ Neutral: {neutral} (below {self.config.price_change_threshold}% threshold)")
        
        if significant_results:
            # Show top 3 signals
            self.log(f"   🏆 Top signals:")
            for i, r in enumerate(significant_results[:5], 1):
                self.log(f"      {i}. {r.symbol}: {r.signal_type.value} | Change: {r.price_change_pct:+.2f}% | Score: {r.score:.2f}")
        else:
            self.log(f"   ⚠️ No significant signals (all below {self.config.price_change_threshold}% threshold)")
        
        self._last_scan_time = datetime.now(timezone.utc)
        self._last_results = results
        
        return results
    
    async def start_periodic_scan(self, symbols: Optional[List[str]] = None):
        """Start periodic scanning"""
        self._running = True
        
        # Get symbols once at start
        if symbols is None:
            symbols = await self.get_symbols()
        
        self.log(f"Starting periodic scan every {self.config.scan_interval_seconds}s for {len(symbols)} pairs")
        
        while self._running:
            try:
                results = await self.scan_all(symbols)
                
                if self.on_scan_complete:
                    self.on_scan_complete(results)
                
                # Wait for next scan interval
                await asyncio.sleep(self.config.scan_interval_seconds)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log(f"Scan error: {e}")
                await asyncio.sleep(60)  # Wait a bit before retrying
    
    async def stop(self):
        """Stop periodic scanning"""
        self._running = False
        if self._scan_task:
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
        self.log("Scanner stopped")
    
    def get_top_signals(self, n: int = 10, signal_filter: Optional[str] = None) -> List[ScanResult]:
        """
        Get top N signals from last scan.
        
        Args:
            n: Number of results to return
            signal_filter: Optional filter - "LONG", "SHORT", or None for all
            
        Returns:
            Top N results matching filter
        """
        results = self._last_results
        
        if signal_filter == "LONG":
            results = [r for r in results if "LONG" in r.signal_type.value]
        elif signal_filter == "SHORT":
            results = [r for r in results if "SHORT" in r.signal_type.value]
        
        return results[:n]
    
    def get_summary(self) -> Dict[str, Any]:
        """Get scan summary"""
        if not self._last_results:
            return {"status": "No scan data"}
        
        long_signals = len([r for r in self._last_results if "LONG" in r.signal_type.value])
        short_signals = len([r for r in self._last_results if "SHORT" in r.signal_type.value])
        neutral = len([r for r in self._last_results if r.signal_type == SignalType.NEUTRAL])
        
        return {
            "last_scan": self._last_scan_time.isoformat() if self._last_scan_time else None,
            "total_pairs": len(self._last_results),
            "long_signals": long_signals,
            "short_signals": short_signals,
            "neutral": neutral,
            "top_long": self.get_top_signals(3, "LONG"),
            "top_short": self.get_top_signals(3, "SHORT")
        }

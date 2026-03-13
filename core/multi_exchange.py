import asyncio
import logging
import time
import threading
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime
from config.constants import Exchange, get_exchange_symbol, Side
from exchanges.base import BaseExchangeClient, FundingRate

logger = logging.getLogger(__name__)

class MultiExchangeManager:
    """Manages connections and positions across multiple exchanges"""
    
    def __init__(self):
        self.clients: Dict[Exchange, BaseExchangeClient] = {}
        self.connected_exchanges: List[Exchange] = []
        
    async def connect_exchange(self, exchange: Exchange, client: BaseExchangeClient) -> bool:
        """Connect to a single exchange"""
        try:
            success = await client.connect()
            if success:
                self.clients[exchange] = client
                self.connected_exchanges.append(exchange)
                await client.set_position_mode(hedge_mode=True)
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to connect to {exchange.value}: {e}")
            return False
    
    async def disconnect_all(self):
        """Disconnect from all exchanges"""
        for exchange, client in self.clients.items():
            try:
                await client.disconnect()
            except Exception as e:
                logger.error(f"Error disconnecting from {exchange.value}: {e}")
        self.clients.clear()
        self.connected_exchanges.clear()
    
    async def get_all_balances(self) -> Dict[Exchange, Any]:
        """Get balances from all connected exchanges"""
        balances = {}
        for exchange, client in self.clients.items():
            try:
                balance = await client.get_balance("USDT")
                balances[exchange] = balance
            except Exception as e:
                logger.error(f"Error getting balance from {exchange.value}: {e}")
        return balances
    
    async def get_funding_rates(self, pair: str) -> Dict[Exchange, FundingRate]:
        """Get funding rates from all exchanges"""
        rates = {}
        for exchange, client in self.clients.items():
            try:
                symbol = get_exchange_symbol(pair, exchange)
                rate = await client.get_funding_rate(symbol)
                rates[exchange] = rate
            except Exception as e:
                logger.error(f"Error getting funding rate from {exchange.value}: {e}")
        return rates
        
    async def get_all_funding_rates(self, pairs: List[str] = None, top_n: int = 0) -> Dict[str, Dict[Exchange, FundingRate]]:
        """Get funding rates for all pairs across all connected exchanges.
        
        If pairs is None, dynamically discovers ALL pairs from bulk exchanges
        (e.g. Binance), sorted by abs(funding_rate), then cross-checks others.
        
        Args:
            pairs: Explicit list of pairs to check. If None, auto-discover.
            top_n: If > 0 and auto-discovering, limit to top N pairs by abs(rate).
        
        Returns: Dict[pair, Dict[Exchange, FundingRate]]
        """
        # ── Step 1: Bulk fetch from all exchanges that support it ──
        bulk_exchange_rates: Dict[Exchange, List[FundingRate]] = {}
        bulk_tasks = []
        bulk_exchanges = []
        for exchange, client in self.clients.items():
            if hasattr(client, "get_all_funding_rates"):
                bulk_tasks.append(client.get_all_funding_rates())
                bulk_exchanges.append(exchange)
                
        if bulk_tasks:
            results = await asyncio.gather(*bulk_tasks, return_exceptions=True)
            for exchange, result in zip(bulk_exchanges, results):
                if isinstance(result, Exception):
                    logger.error(f"Failed to bulk fetch funding rates for {exchange.value}: {result}")
                else:
                    bulk_exchange_rates[exchange] = result

        # ── Step 2: Build the pair list ──
        if pairs is not None:
            # User specified explicit pairs
            target_pairs = pairs
        else:
            # Auto-discover from the first bulk exchange (prefer Binance)
            primary_ex = None
            primary_rates = []
            for ex in [Exchange.BINANCE, Exchange.ASTERDEX]:
                if ex in bulk_exchange_rates:
                    primary_ex = ex
                    primary_rates = bulk_exchange_rates[ex]
                    break
            if not primary_ex and bulk_exchange_rates:
                primary_ex = list(bulk_exchange_rates.keys())[0]
                primary_rates = bulk_exchange_rates[primary_ex]
            
            if not primary_rates:
                logger.warning("No bulk funding rates available to discover pairs.")
                return {}
            
            # Sort by abs(funding_rate) descending
            primary_rates.sort(key=lambda r: abs(r.funding_rate), reverse=True)
            if top_n > 0:
                primary_rates = primary_rates[:top_n]
            
            # Convert exchange symbols to unified pairs (e.g. BTCUSDT -> BTC/USDT)
            target_pairs = []
            for rate in primary_rates:
                sym = rate.symbol
                # Strip exchange suffixes: -SWAP, _USDT -> /USDT, -USDT -> /USDT
                base = sym.replace("-USDT-SWAP", "").replace("-USDT", "").replace("_USDT", "").replace("USDT", "")
                pair = f"{base}/USDT"
                if pair not in target_pairs:
                    target_pairs.append(pair)
            
            logger.info(f"Found {len(bulk_exchange_rates.get(primary_ex, []))} pairs on {primary_ex.value}, using {len(target_pairs)}")

        # ── Step 3: Map bulk rates to target pairs ──
        all_rates: Dict[str, Dict[Exchange, FundingRate]] = {p: {} for p in target_pairs}
        
        for exchange, rates_list in bulk_exchange_rates.items():
            # Build a symbol -> rate lookup
            sym_lookup = {r.symbol: r for r in rates_list}
            for pair in target_pairs:
                ex_symbol = get_exchange_symbol(pair, exchange)
                if ex_symbol in sym_lookup:
                    all_rates[pair][exchange] = sym_lookup[ex_symbol]

        # ── Step 4: Cross-check non-bulk exchanges (batched to avoid rate limits) ──
        BATCH_SIZE = 5
        for exchange, client in self.clients.items():
            if exchange in bulk_exchange_rates:
                continue
            for i in range(0, len(target_pairs), BATCH_SIZE):
                batch = target_pairs[i:i+BATCH_SIZE]
                tasks = []
                for pair in batch:
                    symbol = get_exchange_symbol(pair, exchange)
                    tasks.append(client.get_funding_rate(symbol))
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for pair, result in zip(batch, results):
                    if isinstance(result, Exception):
                        logger.debug(f"Failed to get rate for {pair} on {exchange.value}: {result}")
                    elif result:
                        all_rates[pair][exchange] = result
                if i + BATCH_SIZE < len(target_pairs):
                    await asyncio.sleep(0.5)  # throttle between batches
                    
        # Filter out pairs with no rates
        return {pair: rates for pair, rates in all_rates.items() if rates}
        
    async def subscribe_market_data(self, pair: str, *exchanges: Exchange):
        """Subscribe to market data for the pair on specified exchanges"""
        for exchange in exchanges:
            client = self.clients.get(exchange)
            if not client: continue
            
            try:
                # Determine correct subscription method based on client features
                symbol = get_exchange_symbol(pair, exchange)
                
                # Binance supports bookTicker (best bid/ask)
                if hasattr(client, 'subscribe_book_ticker'):
                    await client.subscribe_book_ticker(symbol)
                # BingX supports depth (we implement depth5 for top of book)
                elif hasattr(client, 'subscribe_depth'):
                    await client.subscribe_depth(symbol)
                # OKX or others might be different (skipped for now)
                
            except Exception as e:
                logger.error(f"Failed to subscribe to {pair} on {exchange.value}: {e}")
    
    async def open_hedged_position(
        self,
        pair: str,
        long_exchange: Exchange,
        short_exchange: Exchange,
        size: float,
        leverage: int
    ) -> Dict[str, Any]:
        """
        Open hedged position between two exchanges with atomic-like behavior.
        If one exchange fails, automatically rollback the other.
        """
        results = {"success": False, "long_order": None, "short_order": None, "error": None}
        
        if long_exchange not in self.clients or short_exchange not in self.clients:
            results["error"] = "One or both exchanges not connected"
            return results
        
        long_client = self.clients[long_exchange]
        short_client = self.clients[short_exchange]
        
        long_symbol = get_exchange_symbol(pair, long_exchange)
        short_symbol = get_exchange_symbol(pair, short_exchange)
        
        # Track what we've done for rollback
        long_order = None
        short_order = None
        leverage_set = False
        
        try:
            logger.info(f"Step 1/3: Pre-flight checks for {pair}...")
            
            # Pre-flight validation: Check balances
            long_balance = await long_client.get_balance("USDT")
            short_balance = await short_client.get_balance("USDT")
            
            if long_balance.available < 10:  # Minimum $10 required
                raise Exception(f"{long_exchange.value}: Insufficient balance (${long_balance.available:.6f})")
            if short_balance.available < 10:
                raise Exception(f"{short_exchange.value}: Insufficient balance (${short_balance.available:.6f})")
            
            logger.info(f"✓ Balance check passed: {long_exchange.value}=${long_balance.available:.6f}, {short_exchange.value}=${short_balance.available:.6f}")
            
            # Pre-flight validation: Check existing positions
            long_pos = await long_client.get_position(long_symbol)
            short_pos = await short_client.get_position(short_symbol)
            
            if long_pos and long_pos.size > 0:
                raise Exception(f"{long_exchange.value}: Already have position for {long_symbol}")
            if short_pos and short_pos.size > 0:
                raise Exception(f"{short_exchange.value}: Already have position for {short_symbol}")
            
            logger.info(f"✓ Position check passed: No existing positions")
            
            # Step 1: Set leverage on both exchanges
            logger.info(f"Step 2/3: Setting leverage to {leverage}x...")
            try:
                await asyncio.gather(
                    long_client.set_leverage(long_symbol, leverage),
                    short_client.set_leverage(short_symbol, leverage)
                )
                leverage_set = True
                logger.info(f"✓ Leverage set successfully")
            except Exception as e:
                raise Exception(f"Failed to set leverage: {e}")
            
            # Step 2: Open LONG position first (safer to fail here before SHORT)
            logger.info(f"Step 3/3: Opening positions...")
            logger.info(f"  → Opening LONG on {long_exchange.value}...")
            
            try:
                long_order = await self._place_order_with_retry(
                    long_client, long_symbol, Side.LONG, size, long_exchange, max_retries=3
                )
                logger.info(f"✓ LONG order placed: {long_order.order_id}")
            except Exception as e:
                # LONG failed after 3 retries - no cleanup needed, just fail
                raise Exception(f"LONG order failed on {long_exchange.value} after 3 retries: {e}")
            
            # Step 3: Open SHORT position
            logger.info(f"  → Opening SHORT on {short_exchange.value}...")
            
            try:
                short_order = await self._place_order_with_retry(
                    short_client, short_symbol, Side.SHORT, size, short_exchange, max_retries=3
                )
                logger.info(f"✓ SHORT order placed: {short_order.order_id}")
            except Exception as e:
                # SHORT failed after 3 retries but LONG succeeded - CRITICAL: Must rollback LONG!
                logger.error(f"✗ SHORT order failed after 3 retries! Attempting to rollback LONG position...")
                
                rollback_success = await self._rollback_long_position(
                    long_client, long_symbol, long_order, long_exchange
                )
                
                if rollback_success:
                    raise Exception(f"SHORT order failed on {short_exchange.value} after 3 retries, LONG position rolled back successfully: {e}")
                else:
                    raise Exception(f"CRITICAL: SHORT order failed AND rollback failed! You have an unhedged LONG position on {long_exchange.value}. Please close manually! Error: {e}")
            
            # Both orders successful
            results["success"] = True
            results["long_order"] = long_order
            results["short_order"] = short_order
            logger.info(f"✅ Hedged position opened successfully!")
            
        except Exception as e:
            results["error"] = str(e)
            logger.error(f"❌ Failed to open hedged position: {e}")
        
        return results
    
    async def _rollback_long_position(
        self, 
        client: BaseExchangeClient, 
        symbol: str, 
        order: Any,
        exchange: Exchange
    ) -> bool:
        """
        Rollback a LONG position by closing it immediately.
        Returns True if successful, False otherwise.
        """
        try:
            logger.warning(f"🔄 Rolling back LONG position on {exchange.value}...")
            
            # Wait a moment for order to settle
            await asyncio.sleep(0.5)
            
            # Verify position exists
            position = await client.get_position(symbol)
            if not position or position.size == 0:
                logger.warning(f"⚠️ No position found on {exchange.value}, might have been rejected")
                return True
            
            # Close the position with retry
            close_order = await self._place_order_with_retry(
                client, symbol, Side.SHORT, position.size, exchange, max_retries=1
            )
            logger.info(f"✓ Rollback successful: Closed LONG position on {exchange.value} (Order: {close_order.order_id})")
            
            return True
            
        except Exception as e:
            logger.error(f"✗ Rollback failed on {exchange.value}: {e}")
            return False
    
    async def _place_order_with_retry(
        self,
        client: BaseExchangeClient,
        symbol: str,
        side: Side,
        size: float,
        exchange: Exchange,
        max_retries: int = 1
    ) -> Any:
        """
        Place order with single attempt (no retries by default for speed).
        Can optionally retry if max_retries > 1.
        """
        last_error = None
        
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    # Exponential backoff: 0.5s, 1s, 2s
                    delay = 0.5 * (2 ** attempt)
                    logger.info(f"  ↻ Retry {attempt}/{max_retries} after {delay}s delay...")
                    await asyncio.sleep(delay)
                
                order = await client.place_market_order(symbol, side, size)
                return order
                
            except Exception as e:
                last_error = e
                logger.warning(f"  ⚠️ Attempt {attempt + 1}/{max_retries} failed: {e}")
                
                if attempt == max_retries - 1:
                    # Last attempt failed
                    raise Exception(f"All {max_retries} attempts failed. Last error: {e}")
        
        # Should not reach here, but just in case
        raise Exception(f"Order failed after {max_retries} retries: {last_error}")
    
    async def close_hedged_position(
        self,
        pair: str,
        long_exchange: Exchange,
        short_exchange: Exchange
    ) -> Dict[str, Any]:
        """Close hedged position"""
        results = {"success": False, "error": None}
        
        long_client = self.clients.get(long_exchange)
        short_client = self.clients.get(short_exchange)
        
        if not long_client or not short_client:
            results["error"] = "Exchange not connected"
            return results
        
        long_symbol = get_exchange_symbol(pair, long_exchange)
        short_symbol = get_exchange_symbol(pair, short_exchange)
        
        try:
            close_tasks = []
            
            # Check if positions exist before closing
            long_pos = await long_client.get_position(long_symbol)
            short_pos = await short_client.get_position(short_symbol)
            
            if long_pos:
                close_tasks.append(long_client.close_position(long_symbol))
            if short_pos:
                close_tasks.append(short_client.close_position(short_symbol))
            
            if close_tasks:
                await asyncio.gather(*close_tasks)
            
            results["success"] = True
            
        except Exception as e:
            results["error"] = str(e)
        
        return results
    
    async def close_hedged_position_split(
        self,
        pair: str,
        long_exchange: Exchange,
        short_exchange: Exchange,
        splits: int = 1,
        interval_seconds: float = 2.0,
        price_spread_min: float = -100.0,
        spread_check_interval: float = 2.0,
        max_wait_per_split: float = 3600.0,
        progress_callback = None,
        cancel_event: Optional[threading.Event] = None
    ) -> Dict[str, Any]:
        """Close hedged position in multiple splits
        
        Args:
            pair: Trading pair
            long_exchange: Exchange with LONG position
            short_exchange: Exchange with SHORT position
            splits: Number of splits to close
            interval_seconds: Seconds between each split
            price_spread_min: Minimum price spread % required before closing each split
            spread_check_interval: Seconds between spread checks when waiting
            max_wait_per_split: Maximum seconds to wait for spread per split
            progress_callback: Callback function(split_num, total_splits, message)
            cancel_event: Optional threading.Event to signal cancellation
        """
        def log_msg(msg: str):
            # Only use callback, don't call logger.info directly
            # because progress_callback -> _log -> logger.info (avoid duplicate)
            if progress_callback:
                progress_callback(0, 0, msg)
            else:
                logger.info(msg)
        
        def is_cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()
            
        results = {
            "success": False, 
            "error": None, 
            "closed_splits": 0,
            "splits_total": splits,
            "cancelled": False
        }
        
        long_client = self.clients.get(long_exchange)
        short_client = self.clients.get(short_exchange)
        
        if not long_client or not short_client:
            results["error"] = "Exchange not connected"
            return results
        
        long_symbol = get_exchange_symbol(pair, long_exchange)
        short_symbol = get_exchange_symbol(pair, short_exchange)
        
        async def check_spread_for_close() -> tuple[bool, float, float, float]:
            """Check if current spread meets threshold for CLOSING.
            
            Logic: 
            - We SELL the LONG position -> fills at BID
            - We BUY the SHORT position -> fills at ASK
            - Spread = (LONG_BID - SHORT_ASK) / avg * 100
            """
            try:
                long_book, short_book = await asyncio.wait_for(
                    asyncio.gather(
                        long_client.get_order_book(long_symbol, limit=10),
                        short_client.get_order_book(short_symbol, limit=10)
                    ),
                    timeout=10.0
                )
                
                if not long_book.get("bids") or len(long_book["bids"]) == 0:
                    return (False, 0.0, 0.0, 0.0)
                long_bid_price = long_book["bids"][0][0]
                
                if not short_book.get("asks") or len(short_book["asks"]) == 0:
                    return (False, 0.0, 0.0, 0.0)
                short_ask_price = short_book["asks"][0][0]
                
                price_diff = long_bid_price - short_ask_price
                avg_price = (long_bid_price + short_ask_price) / 2
                spread_pct = (price_diff / avg_price) * 100
                
                return (spread_pct >= price_spread_min, spread_pct, long_bid_price, short_ask_price)
                
            except Exception as e:
                logger.error(f"Error checking spread for close: {e}")
                return (False, 0.0, 0.0, 0.0)
        
        try:
            long_pos = await long_client.get_position(long_symbol)
            short_pos = await short_client.get_position(short_symbol)
            
            if not long_pos and not short_pos:
                results["error"] = "No positions found to close"
                return results
            
            long_total = long_pos.size if long_pos else 0
            short_total = short_pos.size if short_pos else 0
            
            long_per_split = long_total / splits if long_total > 0 else 0
            short_per_split = short_total / splits if short_total > 0 else 0
            
            # Track current threshold outside loop so it persists across splits
            current_threshold = price_spread_min
            
            for i in range(splits):
                split_num = i + 1
                is_last = (split_num == splits)
                
                if is_cancelled():
                    log_msg(f"🛑 Cancelled before split {split_num}/{splits}")
                    results["cancelled"] = True
                    if results["closed_splits"] > 0:
                        results["success"] = True
                    return results
                
                # Check spread if threshold set
                if price_spread_min > -99.0:
                    log_msg(f"⏳ Split {split_num}: Waiting for spread >= {current_threshold:.4f}%...")
                    wait_start = asyncio.get_event_loop().time()
                    check_count = 0
                    
                    while True:
                        if is_cancelled():
                            log_msg("🛑 Cancelled waiting for spread")
                            results["cancelled"] = True
                            if results["closed_splits"] > 0:
                                results["success"] = True
                            return results
                            
                        is_ok, spread_pct, long_bid, short_ask = await check_spread_for_close()
                        # Check against current (possibly reduced) threshold
                        is_ok = spread_pct >= current_threshold
                        
                        if is_ok:
                            log_msg(f"✅ Split {split_num}: Spread {spread_pct:.4f}% >= {current_threshold:.4f}% - Closing...")
                            break
                        
                        check_count += 1
                        
                        # After 30 checks, reduce threshold by 0.01%
                        if check_count >= 30:
                            old_threshold = current_threshold
                            current_threshold -= 0.01
                            log_msg(f"⚠️ Split {split_num}: 30 lần chưa đạt, giảm threshold: {old_threshold:.4f}% → {current_threshold:.4f}%")
                            check_count = 0
                        
                        elapsed = asyncio.get_event_loop().time() - wait_start
                        if max_wait_per_split > 0 and elapsed >= max_wait_per_split:
                            log_msg(f"⚠️ Timeout waiting for spread. Proceeding to close.")
                            break
                        
                        log_msg(f"📊 Close Spread: {spread_pct:.4f}% < {current_threshold:.4f}% (check {check_count}/30)")
                        await asyncio.sleep(spread_check_interval)
                
                if progress_callback:
                    progress_callback(split_num, splits, f"Closing split {split_num}/{splits}...")
                
                # Wait if we're in the restricted time window (minute 59-01)
                # Some exchanges don't allow orders during funding settlement
                while True:
                    now = datetime.now(timezone.utc)
                    minute = now.minute
                    if minute >= 59 or minute <= 1:
                        if is_cancelled():
                            log_msg(f"🛑 Cancelled while waiting for safe time")
                            results["cancelled"] = True
                            if results["closed_splits"] > 0:
                                results["success"] = True
                            return results
                        log_msg(f"⏳ Split {split_num}: Chờ qua phút {minute} (funding settlement)...")
                        await asyncio.sleep(5)
                    else:
                        break
                
                # Determine sizes to close for this split
                long_size_to_close = 0
                short_size_to_close = 0
                
                if is_last:
                    # Last split: close all remaining
                    if long_pos:
                        try:
                            current_long = await long_client.get_position(long_symbol)
                            if current_long and current_long.size > 0:
                                long_size_to_close = current_long.size
                        except: pass
                    if short_pos:
                        try:
                            current_short = await short_client.get_position(short_symbol)
                            if current_short and current_short.size > 0:
                                short_size_to_close = current_short.size
                        except: pass
                else:
                    long_size_to_close = long_per_split
                    short_size_to_close = short_per_split
                
                # Close LONG first with retries
                long_closed = False
                if long_size_to_close > 0:
                    for attempt in range(3):
                        try:
                            if is_last:
                                await long_client.close_position(long_symbol)
                            else:
                                await long_client.close_position_partial(long_symbol, long_size_to_close)
                            long_closed = True
                            log_msg(f"✓ LONG closed (split {split_num})")
                            break
                        except Exception as e:
                            log_msg(f"⚠️ LONG close attempt {attempt+1}/3 failed: {e}")
                            if attempt < 2:
                                await asyncio.sleep(0.5 * (2 ** attempt))
                    
                    if not long_closed:
                        log_msg(f"❌ LONG close failed after 3 retries. Stopping to prevent volume mismatch.")
                        results["error"] = "LONG close failed after 3 retries"
                        return results
                
                # Close SHORT with retries
                short_closed = False
                if short_size_to_close > 0:
                    for attempt in range(3):
                        try:
                            if is_last:
                                await short_client.close_position(short_symbol)
                            else:
                                await short_client.close_position_partial(short_symbol, short_size_to_close)
                            short_closed = True
                            log_msg(f"✓ SHORT closed (split {split_num})")
                            break
                        except Exception as e:
                            log_msg(f"⚠️ SHORT close attempt {attempt+1}/3 failed: {e}")
                            if attempt < 2:
                                await asyncio.sleep(0.5 * (2 ** attempt))
                    
                    if not short_closed:
                        # SHORT failed but LONG succeeded - try to reopen LONG to maintain hedge
                        log_msg(f"❌ SHORT close failed after 3 retries. Attempting to reopen LONG to maintain hedge...")
                        
                        try:
                            # Reopen LONG position with same size
                            await self._place_order_with_retry(
                                long_client, long_symbol, Side.LONG, long_size_to_close, long_exchange, max_retries=3
                            )
                            log_msg(f"✓ LONG reopened to maintain hedge. Please retry closing later.")
                            results["error"] = "SHORT close failed, LONG reopened to maintain hedge"
                        except Exception as reopen_error:
                            log_msg(f"❌ CRITICAL: SHORT close failed AND LONG reopen failed! Volume mismatch exists. Error: {reopen_error}")
                            results["error"] = f"CRITICAL: Volume mismatch! SHORT close failed, LONG reopen failed: {reopen_error}"
                        
                        return results
                
                results["closed_splits"] = split_num
                
                if not is_last and interval_seconds > 0:
                    log_msg(f"⏳ Waiting {interval_seconds}s before next split...")
                    await asyncio.sleep(interval_seconds)
            
            results["success"] = True
            
        except Exception as e:
            results["error"] = str(e)
            logger.error(f"Error closing position: {e}")
        
        return results
    
    async def _cleanup_partial(self, ex1: Exchange, ex2: Exchange, pair: str):
        """Cleanup partial positions"""
        for ex in [ex1, ex2]:
            try:
                client = self.clients.get(ex)
                if client:
                    symbol = get_exchange_symbol(pair, ex)
                    pos = await client.get_position(symbol)
                    if pos:
                        await client.close_position(symbol)
            except:
                pass
    
    async def check_positions(self, pair: str, long_ex: Exchange, short_ex: Exchange) -> Dict[str, Any]:
        """Check positions on both exchanges
        
        Returns:
            Dict with 'long', 'short' positions and 'one_side_missing' flag
            Note: Does NOT determine liquidation - caller should retry to confirm
        """
        result = {"long": None, "short": None, "one_side_missing": None}
        
        try:
            long_client = self.clients.get(long_ex)
            short_client = self.clients.get(short_ex)
            
            # Get positions with timeout to prevent hanging
            if long_client:
                symbol = get_exchange_symbol(pair, long_ex)
                try:
                    result["long"] = await asyncio.wait_for(
                        long_client.get_position(symbol, force_rest=True),
                        timeout=10.0
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"Timeout getting position from {long_ex.value}")
                    result["long"] = None
            
            if short_client:
                symbol = get_exchange_symbol(pair, short_ex)
                try:
                    result["short"] = await asyncio.wait_for(
                        short_client.get_position(symbol, force_rest=True),
                        timeout=10.0
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"Timeout getting position from {short_ex.value}")
                    result["short"] = None
            
            # Check if one side is missing (potential liquidation)
            long_exists = result["long"] is not None and result["long"].size > 0
            short_exists = result["short"] is not None and result["short"].size > 0
            
            # Flag which side is missing (if any)
            if not long_exists and short_exists:
                result["one_side_missing"] = long_ex
            elif not short_exists and long_exists:
                result["one_side_missing"] = short_ex
            # If both missing or both exist, one_side_missing = None
                
        except Exception as e:
            logger.error(f"Error checking positions: {e}")
        
        return result
    
    async def scan_all_positions(self) -> Dict[Exchange, List[Any]]:
        """
        Scan all positions from all connected exchanges
        
        Returns:
            Dict mapping Exchange to list of positions
        """
        all_positions = {}
        
        for exchange, client in self.clients.items():
            try:
                # Add timeout per exchange to prevent hanging
                positions = await asyncio.wait_for(
                    client.get_all_positions(),
                    timeout=15.0
                )
                all_positions[exchange] = positions
                logger.info(f"Found {len(positions)} positions on {exchange.value}")
            except asyncio.TimeoutError:
                logger.error(f"Timeout scanning positions from {exchange.value}")
                all_positions[exchange] = []
            except Exception as e:
                logger.error(f"Error scanning positions from {exchange.value}: {e}")
                all_positions[exchange] = []
        
        return all_positions
    
    async def analyze_spread(
        self,
        pair: str,
        long_exchange: Exchange,
        short_exchange: Exchange,
        duration_seconds: float = 300.0,  # 5 phút
        check_interval: float = 2.0,      # 2 giây
        log_callback = None,
        cancel_event: Optional[threading.Event] = None,
        mode: str = "open"  # "open" hoặc "close"
    ) -> Dict[str, Any]:
        """
        Analyze spread trong khoảng thời gian, trả về giá trị cao thứ 2.
        
        Args:
            pair: Trading pair
            long_exchange: Exchange có LONG position
            short_exchange: Exchange có SHORT position
            duration_seconds: Thời gian analyze (mặc định 5 phút)
            check_interval: Khoảng cách giữa các lần check (mặc định 2 giây)
            log_callback: Callback để log ra GUI
            cancel_event: Event để cancel analyze
            mode: "open" = tính spread mở (SHORT_BID - LONG_ASK), 
                  "close" = tính spread đóng (LONG_BID - SHORT_ASK)
        
        Returns:
            Dict với:
                - success: bool
                - second_best_spread: float (giá trị cao thứ 2)
                - best_spread: float (giá trị cao nhất)
                - spreads: List[float] (tất cả spreads thu thập được)
                - samples: int (số lượng samples)
                - error: str nếu có lỗi
        """
        def log_msg(msg: str):
            # Only use callback to avoid duplicate logs
            if log_callback:
                log_callback(msg)
            else:
                logger.info(msg)
        
        def is_cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()
        
        result = {
            "success": False,
            "second_best_spread": None,
            "best_spread": None,
            "spreads": [],
            "samples": 0,
            "error": None
        }
        
        if long_exchange not in self.clients or short_exchange not in self.clients:
            result["error"] = "One or both exchanges not connected"
            return result
        
        long_client = self.clients[long_exchange]
        short_client = self.clients[short_exchange]
        
        long_symbol = get_exchange_symbol(pair, long_exchange)
        short_symbol = get_exchange_symbol(pair, short_exchange)
        
        spreads = []
        start_time = asyncio.get_event_loop().time()
        sample_count = 0
        expected_samples = int(duration_seconds / check_interval)
        
        log_msg(f"📊 Bắt đầu analyze spread {duration_seconds:.0f}s ({expected_samples} samples dự kiến)...")
        
        while True:
            if is_cancelled():
                log_msg("🛑 Analyze bị cancel")
                result["error"] = "Cancelled"
                return result
            
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= duration_seconds:
                break
            
            try:
                # Fetch order books
                long_book, short_book = await asyncio.wait_for(
                    asyncio.gather(
                        long_client.get_order_book(long_symbol, limit=10),
                        short_client.get_order_book(short_symbol, limit=10)
                    ),
                    timeout=10.0
                )
                
                if mode == "open":
                    # Open: LONG (BUY) fills at ASK, SHORT (SELL) fills at BID
                    # Spread = SHORT_BID - LONG_ASK
                    if long_book.get("asks") and short_book.get("bids"):
                        long_ask = long_book["asks"][0][0]
                        short_bid = short_book["bids"][0][0]
                        avg_price = (long_ask + short_bid) / 2
                        spread_pct = ((short_bid - long_ask) / avg_price) * 100
                        spreads.append(spread_pct)
                        sample_count += 1
                else:
                    # Close: LONG (SELL) fills at BID, SHORT (BUY) fills at ASK
                    # Spread = LONG_BID - SHORT_ASK
                    if long_book.get("bids") and short_book.get("asks"):
                        long_bid = long_book["bids"][0][0]
                        short_ask = short_book["asks"][0][0]
                        avg_price = (long_bid + short_ask) / 2
                        spread_pct = ((long_bid - short_ask) / avg_price) * 100
                        spreads.append(spread_pct)
                        sample_count += 1
                
                # Log progress mỗi 30 giây
                if sample_count % 15 == 0:  # 15 samples * 2s = 30s
                    remaining = duration_seconds - elapsed
                    log_msg(f"📊 Analyzing... {sample_count} samples | {remaining:.0f}s còn lại | Spread hiện tại: {spreads[-1]:.4f}%")
                
            except asyncio.TimeoutError:
                logger.warning("Timeout fetching order book during analyze")
            except Exception as e:
                logger.error(f"Error during analyze: {e}")
            
            await asyncio.sleep(check_interval)
        
        if len(spreads) < 2:
            result["error"] = f"Không đủ samples (chỉ có {len(spreads)})"
            return result
        
        # Sort descending để lấy giá trị cao nhất
        sorted_spreads = sorted(spreads, reverse=True)
        best_spread = sorted_spreads[0]
        second_best_spread = sorted_spreads[2]
        
        result["success"] = True
        result["spreads"] = spreads
        result["samples"] = len(spreads)
        result["best_spread"] = best_spread
        result["second_best_spread"] = second_best_spread
        
        log_msg(f"✅ Analyze hoàn thành: {len(spreads)} samples")
        log_msg(f"   Best: {best_spread:.4f}% | Second best: {second_best_spread:.4f}%")
        log_msg(f"   Min: {min(spreads):.4f}% | Max: {max(spreads):.4f}% | Avg: {sum(spreads)/len(spreads):.4f}%")
        
        return result
    
    async def open_hedged_position_split(
        self,
        pair: str,
        long_exchange: Exchange,
        short_exchange: Exchange,
        total_size: float,
        leverage: int,
        split_count: int = 1,
        delay_between_splits: float = 0.5,
        price_spread_min: float = 0.0,
        spread_check_interval: float = 2.0,
        max_wait_per_split: float = 300.0,
        log_callback = None,
        on_first_split_complete = None,
        skip_leverage_set: bool = False,
        cancel_event: Optional[threading.Event] = None
    ) -> Dict[str, Any]:
        """
        Open hedged position in multiple splits (DCA style)
        Checks price spread threshold before each split.
        
        Args:
            pair: Trading pair
            long_exchange: Exchange for LONG
            short_exchange: Exchange for SHORT
            total_size: Total position size
            leverage: Leverage to use
            split_count: Number of splits (1, 3, 5, 10)
            delay_between_splits: Delay in seconds between each split
            price_spread_min: Minimum price spread % required before opening each split
            spread_check_interval: Seconds between spread checks when waiting
            max_wait_per_split: Maximum seconds to wait for spread per split (0 = unlimited)
            log_callback: Optional callback function for logging to GUI
            on_first_split_complete: Optional callback called after first split completes (to start monitoring)
            skip_leverage_set: Skip setting leverage (use if leverage already set)
            cancel_event: Optional threading.Event to signal cancellation (thread-safe)
        
        Returns:
            Dict with success status and details
        """
        def log_msg(msg: str, force: bool = False):
            """Log message - use force=True for important messages"""
            # Only use callback to avoid duplicate logs
            if log_callback and force:
                log_callback(msg)
            elif not log_callback:
                logger.info(msg)
        
        def log_important(msg: str):
            """Always log to GUI (or logger if no callback)"""
            if log_callback:
                log_callback(msg)
            else:
                logger.info(msg)
        
        def is_cancelled() -> bool:
            """Check if cancellation was requested"""
            return cancel_event is not None and cancel_event.is_set()
        
        results = {
            "success": False, 
            "splits_completed": 0,
            "splits_total": split_count,
            "total_long_size": 0,
            "total_short_size": 0,
            "error": None,
            "split_results": [],
            "cancelled": False
        }
        
        if long_exchange not in self.clients or short_exchange not in self.clients:
            results["error"] = "One or both exchanges not connected"
            return results
        
        long_client = self.clients[long_exchange]
        short_client = self.clients[short_exchange]
        
        long_symbol = get_exchange_symbol(pair, long_exchange)
        short_symbol = get_exchange_symbol(pair, short_exchange)
        
        # Calculate size per split
        size_per_split = total_size / split_count
        
        async def check_spread() -> tuple[bool, float, float, float]:
            """Check if current spread meets threshold using order book (bid/ask prices).
            
            Returns (is_ok, spread_pct, long_ask_price, short_bid_price)
            
            Logic: 
            - LONG (BUY market) will fill at ASK price (sellers)
            - SHORT (SELL market) will fill at BID price (buyers)
            - Spread = (SHORT_BID - LONG_ASK) / avg_price * 100
            - For funding arbitrage, negative spread is OK since we profit from funding fees
            """
            try:
                # Get order books with timeout
                long_book, short_book = await asyncio.wait_for(
                    asyncio.gather(
                        long_client.get_order_book(long_symbol, limit=10),
                        short_client.get_order_book(short_symbol, limit=10)
                    ),
                    timeout=10.0
                )
                
                # Get best ask (lowest sell price) from LONG exchange
                # This is the price we'll pay when buying (opening LONG)
                if not long_book.get("asks") or len(long_book["asks"]) == 0:
                    logger.warning(f"No asks in order book for {long_symbol}")
                    return (False, 0.0, 0.0, 0.0)
                long_ask_price = long_book["asks"][0][0]  # Best ask [price, qty]
                
                # Get best bid (highest buy price) from SHORT exchange
                # This is the price we'll receive when selling (opening SHORT)
                if not short_book.get("bids") or len(short_book["bids"]) == 0:
                    logger.warning(f"No bids in order book for {short_symbol}")
                    return (False, 0.0, 0.0, 0.0)
                short_bid_price = short_book["bids"][0][0]  # Best bid [price, qty]
                
                # Calculate spread percentage
                # Positive = profitable on entry, Negative = loss on entry (but OK for funding arb)
                price_diff = short_bid_price - long_ask_price
                avg_price = (long_ask_price + short_bid_price) / 2
                spread_pct = (price_diff / avg_price) * 100
                
                # For funding arbitrage, we allow negative spread
                # User sets price_spread_min to control minimum acceptable spread
                # If price_spread_min = -0.1, allows up to -0.1% spread
                return (spread_pct >= price_spread_min, spread_pct, long_ask_price, short_bid_price)
                
            except asyncio.TimeoutError:
                logger.warning("Timeout checking spread")
                return (False, 0.0, 0.0, 0.0)
            except Exception as e:
                logger.error(f"Error checking spread: {e}")
                return (False, 0.0, 0.0, 0.0)
        
        try:
            # Set leverage first (skip if already set to save time)
            if not skip_leverage_set:
                log_msg(f"Setting leverage to {leverage}x on both exchanges...")
                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            long_client.set_leverage(long_symbol, leverage),
                            short_client.set_leverage(short_symbol, leverage)
                        ),
                        timeout=15.0
                    )
                except asyncio.TimeoutError:
                    log_important("⚠️ Timeout setting leverage, continuing anyway...")
            
            # Determine log frequency based on split count
            # For many splits, only log every N splits to reduce GUI spam
            log_every = 1 if split_count <= 10 else (5 if split_count <= 50 else 10)
            
            # Track current threshold (can be reduced if spread not met after 30 checks)
            current_threshold = price_spread_min
            
            # Open positions in splits
            for i in range(split_count):
                split_num = i + 1
                should_log_this_split = (split_num == 1 or split_num == split_count or split_num % log_every == 0)
                
                # Check for cancellation before each split
                if is_cancelled():
                    log_important(f"🛑 Cancelled before split {split_num}/{split_count}")
                    results["cancelled"] = True
                    results["error"] = "Cancelled by user"
                    if results["splits_completed"] > 0:
                        results["success"] = True
                    return results
                
                # Yield to event loop briefly to keep GUI responsive
                await asyncio.sleep(0.01)
                
                # Check spread before each split (except first one which was already checked)
                # Always check spread regardless of threshold being positive or negative
                if i > 0:
                    # Only log waiting message for first few splits or every N splits
                    if should_log_this_split:
                        log_important(f"⏳ Split {split_num}/{split_count}: Checking spread...")
                    wait_start = asyncio.get_event_loop().time()
                    check_count = 0  # Counter for threshold reduction
                    
                    while True:
                        # Check for cancellation while waiting for spread
                        if is_cancelled():
                            log_important(f"🛑 Cancelled while waiting for spread (split {split_num}/{split_count})")
                            results["cancelled"] = True
                            results["error"] = "Cancelled by user"
                            if results["splits_completed"] > 0:
                                results["success"] = True
                            return results
                        
                        is_ok, spread_pct, long_price, short_price = await check_spread()
                        
                        # Check against current threshold (not original)
                        is_ok = spread_pct >= current_threshold
                        
                        if is_ok:
                            # Only log spread OK for important splits
                            log_msg(f"✅ Split {split_num}: Spread {spread_pct:.4f}% >= {current_threshold:.4f}% OK", force=should_log_this_split)
                            break
                        
                        check_count += 1
                        
                        # After 30 checks, reduce threshold by 0.01%
                        if check_count >= 30:
                            old_threshold = current_threshold
                            current_threshold -= 0.01
                            log_important(f"⚠️ Split {split_num}: 30 lần chưa đạt, giảm threshold: {old_threshold:.4f}% → {current_threshold:.4f}%")
                            check_count = 0  # Reset counter
                        
                        # Check timeout
                        elapsed = asyncio.get_event_loop().time() - wait_start
                        if max_wait_per_split > 0 and elapsed >= max_wait_per_split:
                            log_important(f"⚠️ Split {split_num}: Timeout waiting for spread ({elapsed:.0f}s). Skipping remaining splits.")
                            results["error"] = f"Timeout waiting for spread at split {split_num}"
                            # Return with whatever splits we completed
                            if results["splits_completed"] > 0:
                                results["success"] = True
                            return results
                        
                        # Don't log every spread check - too spammy
                        log_msg(f"📊 Split {split_num}: Spread {spread_pct:.4f}% < {current_threshold:.4f}% (check {check_count}/30)", force=False)
                        await asyncio.sleep(spread_check_interval)
                
                # Only log opening message for important splits
                log_msg(f"🔄 Opening split {split_num}/{split_count} (size: {size_per_split})...", force=should_log_this_split)
                
                # Wait if we're in the restricted time window (minute 59-01)
                # Some exchanges don't allow orders during funding settlement
                while True:
                    now = datetime.now(timezone.utc)
                    minute = now.minute
                    if minute >= 59 or minute <= 1:
                        if is_cancelled():
                            log_important(f"🛑 Cancelled while waiting for safe time")
                            results["cancelled"] = True
                            results["error"] = "Cancelled by user"
                            if results["splits_completed"] > 0:
                                results["success"] = True
                            return results
                        log_msg(f"⏳ Split {split_num}: Chờ qua phút {minute} (funding settlement)...", force=should_log_this_split)
                        await asyncio.sleep(5)  # Check every 5 seconds
                    else:
                        break
                
                try:
                    # Open both sides simultaneously for this split with timeout
                    long_order, short_order = await asyncio.wait_for(
                        asyncio.gather(
                            long_client.place_market_order(long_symbol, Side.LONG, size_per_split),
                            short_client.place_market_order(short_symbol, Side.SHORT, size_per_split)
                        ),
                        timeout=30.0
                    )
                    
                    results["splits_completed"] += 1
                    results["total_long_size"] += size_per_split
                    results["total_short_size"] += size_per_split
                    results["split_results"].append({
                        "split": split_num,
                        "success": True,
                        "long_order": long_order.order_id if long_order else None,
                        "short_order": short_order.order_id if short_order else None
                    })
                    
                    # Only log completion for important splits
                    log_msg(f"✓ Split {split_num}/{split_count} completed", force=should_log_this_split)
                    
                    # Call callback after first split to start monitoring
                    if split_num == 1 and on_first_split_complete:
                        try:
                            on_first_split_complete(size_per_split)
                        except Exception as e:
                            logger.error(f"Error in on_first_split_complete callback: {e}")
                    
                    # Delay between splits (except for last one) - don't log delay
                    if i < split_count - 1:
                        await asyncio.sleep(delay_between_splits)
                
                except asyncio.TimeoutError:
                    log_important(f"⚠️ Split {split_num} timeout - retrying once...")
                    
                    # Wait if we're in the restricted time window before retry
                    while True:
                        now = datetime.now(timezone.utc)
                        minute = now.minute
                        if minute >= 59 or minute <= 1:
                            log_msg(f"⏳ Split {split_num}: Chờ qua phút {minute} (funding settlement)...", force=True)
                            await asyncio.sleep(5)
                        else:
                            break
                    
                    # Retry once on timeout
                    try:
                        long_order, short_order = await asyncio.wait_for(
                            asyncio.gather(
                                long_client.place_market_order(long_symbol, Side.LONG, size_per_split),
                                short_client.place_market_order(short_symbol, Side.SHORT, size_per_split)
                            ),
                            timeout=30.0
                        )
                        results["splits_completed"] += 1
                        results["total_long_size"] += size_per_split
                        results["total_short_size"] += size_per_split
                        results["split_results"].append({
                            "split": split_num,
                            "success": True,
                            "long_order": long_order.order_id if long_order else None,
                            "short_order": short_order.order_id if short_order else None
                        })
                        log_msg(f"✓ Split {split_num}/{split_count} completed (retry)", force=should_log_this_split)
                        
                        if split_num == 1 and on_first_split_complete:
                            try:
                                on_first_split_complete(size_per_split)
                            except Exception as e:
                                logger.error(f"Error in on_first_split_complete callback: {e}")
                        
                        if i < split_count - 1:
                            await asyncio.sleep(delay_between_splits)
                    except Exception as retry_e:
                        logger.error(f"✗ Split {split_num} failed on retry: {retry_e}")
                        results["split_results"].append({
                            "split": split_num,
                            "success": False,
                            "error": str(retry_e)
                        })
                        
                        # === REROLL ON FAILURE ===
                        long_pos = await long_client.get_position(long_symbol, force_rest=True)
                        short_pos = await short_client.get_position(short_symbol, force_rest=True)
                        
                        long_actual = long_pos.size if long_pos else 0
                        short_actual = short_pos.size if short_pos else 0
                        size_diff = abs(long_actual - short_actual)
                        
                        if size_diff > 0.0001:
                            reroll_success = False
                            for reroll_attempt in range(1, 4):
                                diff = long_actual - short_actual
                                log_important(f"⚠️ Mismatch: Long={long_actual:.6f}, Short={short_actual:.6f}. Reroll #{reroll_attempt}")
                                
                                try:
                                    if diff > 0:
                                        await short_client.place_market_order(short_symbol, Side.SHORT, abs(diff))
                                    else:
                                        await long_client.place_market_order(long_symbol, Side.LONG, abs(diff))
                                    
                                    await asyncio.sleep(1)
                                    
                                    long_pos = await long_client.get_position(long_symbol, force_rest=True)
                                    short_pos = await short_client.get_position(short_symbol, force_rest=True)
                                    long_actual = long_pos.size if long_pos else 0
                                    short_actual = short_pos.size if short_pos else 0
                                    size_diff = abs(long_actual - short_actual)
                                    
                                    if size_diff <= 0.0001:
                                        log_msg(f"✓ Reroll thành công sau {reroll_attempt} lần", force=True)
                                        reroll_success = True
                                        break
                                except Exception as reroll_e:
                                    logger.error(f"Reroll #{reroll_attempt} failed: {reroll_e}")
                            
                            if not reroll_success:
                                log_important(f"❌ Reroll failed 3 lần. STOP. Long={long_actual:.6f}, Short={short_actual:.6f}")
                                results["error"] = "Reroll failed after 3 attempts"
                                results["success"] = results["splits_completed"] > 0
                                break
                        # === END REROLL ===
                        
                except Exception as e:
                    logger.error(f"✗ Split {split_num} failed: {e}")
                    log_important(f"❌ Split {split_num} failed: {e}")
                    results["split_results"].append({
                        "split": split_num,
                        "success": False,
                        "error": str(e)
                    })
                    
                    # === REROLL ON FAILURE ===
                    long_pos = await long_client.get_position(long_symbol, force_rest=True)
                    short_pos = await short_client.get_position(short_symbol, force_rest=True)
                    
                    long_actual = long_pos.size if long_pos else 0
                    short_actual = short_pos.size if short_pos else 0
                    size_diff = abs(long_actual - short_actual)
                    
                    if size_diff > 0.0001:
                        reroll_success = False
                        for reroll_attempt in range(1, 4):
                            diff = long_actual - short_actual
                            log_important(f"⚠️ Mismatch: Long={long_actual:.6f}, Short={short_actual:.6f}. Reroll #{reroll_attempt}")
                            
                            try:
                                if diff > 0:
                                    await short_client.place_market_order(short_symbol, Side.SHORT, abs(diff))
                                else:
                                    await long_client.place_market_order(long_symbol, Side.LONG, abs(diff))
                                
                                await asyncio.sleep(1)
                                
                                long_pos = await long_client.get_position(long_symbol, force_rest=True)
                                short_pos = await short_client.get_position(short_symbol, force_rest=True)
                                long_actual = long_pos.size if long_pos else 0
                                short_actual = short_pos.size if short_pos else 0
                                size_diff = abs(long_actual - short_actual)
                                
                                if size_diff <= 0.0001:
                                    log_msg(f"✓ Reroll thành công sau {reroll_attempt} lần", force=True)
                                    reroll_success = True
                                    break
                            except Exception as reroll_e:
                                logger.error(f"Reroll #{reroll_attempt} failed: {reroll_e}")
                        
                        if not reroll_success:
                            log_important(f"❌ Reroll failed 3 lần. STOP. Long={long_actual:.6f}, Short={short_actual:.6f}")
                            results["error"] = "Reroll failed after 3 attempts"
                            results["success"] = results["splits_completed"] > 0
                            break
                    # === END REROLL ===
            
            # Consider success if at least one split completed
            if results["splits_completed"] > 0:
                results["success"] = True
                log_important(f"✅ Completed {results['splits_completed']}/{split_count} splits")
            else:
                results["error"] = "All splits failed"
                
        except Exception as e:
            results["error"] = str(e)
            logger.error(f"❌ Failed to open hedged position: {e}")
        
        logger.debug(f"open_hedged_position_split: EXIT with success={results.get('success')}")
        return results

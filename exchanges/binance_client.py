"""
Binance Futures API Client
"""
import hmac
import hashlib
import time
import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from urllib.parse import urlencode
import aiohttp

from config.settings import BinanceConfig
from config.constants import Side, OrderType, PositionStatus, Exchange, get_exchange_symbol
from .base import BaseExchangeClient, Position, Order, FundingRate, Balance

logger = logging.getLogger(__name__)


class BinanceClient(BaseExchangeClient):
    """Binance Futures API Client"""
    
    
    def __init__(self, config: BinanceConfig, debug: bool = False):
        super().__init__(config.api_key, config.secret_key)
        self.config = config
        self.base_url = config.base_url
        self._session: Optional[aiohttp.ClientSession] = None
        self.debug = debug
        
        # WebSocket state
        from .websocket_manager import WebSocketManager
        self.ws_manager = WebSocketManager(config.ws_url)
        self.ws_manager.add_callback(self._handle_ws_message)
        self._listen_key = None
        self._listen_key_timer = None
        
        # Caches
        self._ticker_cache: Dict[str, Dict] = {}  # symbol -> {bid, ask, time}
        self._position_cache: Dict[str, Position] = {} # symbol -> Position
        self._order_cache: Dict[str, Order] = {} # order_id -> Order
        self._funding_info_cache: list = []  # cached /fapi/v1/fundingInfo response
        self._funding_info_cache_time: float = 0  # timestamp of last cache
        self.hedge_mode = False  # One-Way mode (synced from API on connect)
        
    async def connect(self) -> bool:
        """Connect to Binance API and WS"""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        
        try:
            balance = await self.get_balance()
            
            # Sync position mode
            await self._sync_position_mode()
            
            # Start User Stream (ListenKey)
            await self.start_user_stream()
            
            return balance is not None
        except Exception as e:
            print(f"Binance connection error: {e}")
            if self._session:
                await self._session.close()
                self._session = None
            return False
            
    async def _sync_position_mode(self):
        """Sync current position mode from exchange"""
        try:
            # Endpoint returns something like {"feeTier":0,"canTrade":true,"canDeposit":true,"canWithdraw":true,"updateTime":0,"multiAssetsMargin":false,"tradeGroupId":-1,"options1":-1,"options2":-1,"options3":-1,"options4":-1,"feeBurn":false,"dualSidePosition":true}
            # Or GET /fapi/v1/positionSide/dual returns {"dualSidePosition": true}
            result = await self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
            self.hedge_mode = result.get("dualSidePosition", True)
            logger.info(f"Binance position mode synced: {'Hedge' if self.hedge_mode else 'One-Way'}")
        except Exception as e:
            logger.warning(f"Failed to sync Binance position mode: {e}")
            
    async def disconnect(self):
        """Disconnect from Binance API"""
        if self._listen_key_timer:
            self._listen_key_timer.cancel()
            
        await self.ws_manager.disconnect()
        
        if self._session:
            await self._session.close()
            import asyncio
            await asyncio.sleep(0.25)
            self._session = None
            
    async def start_user_stream(self):
        """Start User Data Stream"""
        try:
            # Get Listen Key
            res = await self._request("POST", "/fapi/v1/listenKey", signed=True)
            self._listen_key = res["listenKey"]
            logger.info(f"Binance ListenKey: {self._listen_key}")
            
            # Update WS URL with ListenKey (Binance User Stream specific)
            # Binance separates Base WS URL and User Stream, but typically we can append
            # For this simple implementation, we might need a separate WS connection 
            # or just one if we can multiplex. 
            # Binance Futures typically: wss://fstream.binance.com/ws/<listenKey> for User
            # And wss://fstream.binance.com/ws/bnbusdt@bookTicker for Market
            # They CAN be combined: wss://fstream.binance.com/stream?streams=<listenKey>/bnbusdt@bookTicker
            
            # Re-init WS Manager with multiplex URL structure
            base_ws = self.config.ws_url + "/stream?streams=" + self._listen_key
            self.ws_manager.url = base_ws
            
            await self.ws_manager.connect()
            
            # Schedule Keepalive (every 30 mins)
            self._schedule_listen_key_keepalive()
            
        except Exception as e:
            logger.error(f"Failed to start user stream: {e}")

    def _schedule_listen_key_keepalive(self):
        async def keepalive():
            while True:
                await asyncio.sleep(1800)  # 30 mins
                try:
                    await self._request("PUT", "/fapi/v1/listenKey", signed=True)
                    logger.debug("Binance ListenKey refreshed")
                except Exception as e:
                    logger.error(f"Binance ListenKey refresh failed: {e}")
                    
        self._listen_key_timer = asyncio.create_task(keepalive())

    async def subscribe_book_ticker(self, symbol: str):
        """Subscribe to best bid/ask for symbol via WS"""
        binance_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BINANCE)
        # Stream name needs to be lowercase for Binance
        stream_name = f"{binance_symbol.lower()}@bookTicker"
        
        payload = {
            "method": "SUBSCRIBE",
            "params": [stream_name],
            "id": int(time.time())
        }
        await self.ws_manager.subscribe(payload)
        logger.info(f"Subscribed to {stream_name}")

    def _handle_ws_message(self, msg: Dict):
        """Process incoming WS messages"""
        # Determine message type
        stream = msg.get("stream", "")
        data = msg.get("data", msg)
        
        # 1. Book Ticker (Spread updates)
        if "@bookTicker" in stream:
            # {"u":400900217,"s":"BNBUSDT","b":"25.35190","B":"31.21","a":"25.36520","A":"40.66"...}
            symbol = data.get("s")
            try:
                self._ticker_cache[symbol] = {
                    "bid": float(data.get("b", 0)),
                    "bid_qty": float(data.get("B", 0)),
                    "ask": float(data.get("a", 0)),
                    "ask_qty": float(data.get("A", 0)),
                    "time": time.time()
                }
            except Exception as e:
                logger.error(f"Error parsing bookTicker: {e}")
                 
        # 2. User Data Update (ORDER_TRADE_UPDATE, ACCOUNT_UPDATE)
        event_type = data.get("e")
        
        if event_type == "ACCOUNT_UPDATE":
            # Position updates
            update_data = data.get("a", {})
            positions = update_data.get("P", [])
            for p in positions:
                symbol = p.get("s")
                amt = float(p.get("pa", 0))
                entry = float(p.get("ep", 0))
                upnl = float(p.get("up", 0))
                
                # Check if we have this position in cache or create new
                if amt == 0:
                    if symbol in self._position_cache:
                        del self._position_cache[symbol]
                else:
                    side = Side.LONG if amt > 0 else Side.SHORT
                    
                    # Update cache
                    self._position_cache[symbol] = Position(
                        symbol=symbol,
                        side=side,
                        size=abs(amt),
                        entry_price=entry,
                        mark_price=0, 
                        liquidation_price=0, 
                        unrealized_pnl=upnl,
                        leverage=1,
                        status=PositionStatus.OPEN,
                        timestamp=datetime.now(timezone.utc),
                        raw_data=p
                    )

        elif event_type == "ORDER_TRADE_UPDATE":
            # Order status update
            o = data.get("o", {})
            symbol = o.get("s")
            status = o.get("X") # NEW, FILLED, CANCELED
            order_id = str(o.get("i"))
            
            logger.info(f"WS: Order {order_id} ({symbol}) update: {status}")

    async def get_mark_price(self, symbol: str) -> float:
        """Get current mark price"""
        binance_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BINANCE)
        
        result = await self._request("GET", "/fapi/v1/premiumIndex", {"symbol": binance_symbol}, signed=False)
        
        return float(result.get("markPrice", 0))
    
    async def get_order_book(self, symbol: str, limit: int = 20) -> Dict[str, Any]:
        """Get order book (depth)"""
        binance_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BINANCE)
        
        # 1. OPTIMIZATION: Check Ticker Cache for Best Bid/Ask
        # If we have recent data from WS, return it as a 1-depth orderbook
        # This is sufficient for spread checking which only looks at bids[0] and asks[0]
        if binance_symbol in self._ticker_cache:
            ticker = self._ticker_cache[binance_symbol]
            # Check if cache is fresh (e.g. < 5s)
            if time.time() - ticker.get("time", 0) < 5.0:
                 return {
                     "bids": [[ticker["bid"], ticker["bid_qty"]]],
                     "asks": [[ticker["ask"], ticker["ask_qty"]]]
                 }
        
        # 2. Fallback to REST
        result = await self._request("GET", "/fapi/v1/depth", {"symbol": binance_symbol, "limit": limit}, signed=False)
        return {
            "bids": [[float(price), float(qty)] for price, qty in result["bids"]],
            "asks": [[float(price), float(qty)] for price, qty in result["asks"]]
        }

    def _get_timestamp(self) -> int:
        """Get current timestamp in milliseconds"""
        return int(time.time() * 1000)
    
    def _sign(self, params: Dict[str, Any]) -> str:
        """Generate signature for API request"""
        query_string = urlencode(params)
        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature
    
    def _get_headers(self) -> Dict[str, str]:
        """Get headers for API request"""
        return {
            "X-MBX-APIKEY": self.api_key,
            "Content-Type": "application/json"
        }
    

    
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        signed: bool = True,
        timeout: float = 15.0
    ) -> Any:
        """Make API request with timeout"""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        
        url = f"{self.base_url}{endpoint}"
        params = params or {}
        
        if signed:
            params["timestamp"] = self._get_timestamp()
            params["recvWindow"] = 60000  # Increase to 60 seconds for slow connections
            params["signature"] = self._sign(params)
        
        headers = self._get_headers()
        
        # Debug: Print request details
        if self.debug:
            print(f"\n{'='*60}")
            print(f"[BINANCE REQUEST]")
            print(f"  Method: {method}")
            print(f"  URL: {url}")
            print(f"  Params: {json.dumps({k: v for k, v in params.items() if k != 'signature'}, indent=2)}")
            print(f"  Headers: X-MBX-APIKEY: {self.api_key[:8]}...")
        
        # Create timeout object
        req_timeout = aiohttp.ClientTimeout(total=timeout)
        
        try:
            if method == "GET":
                async with self._session.get(url, headers=headers, params=params, timeout=req_timeout) as response:
                    status_code = response.status
                    result = await response.json()
            else:
                async with self._session.request(method, url, headers=headers, params=params, timeout=req_timeout) as response:
                    status_code = response.status
                    result = await response.json()
        except asyncio.TimeoutError:
            raise Exception(f"Binance API request timeout after {timeout}s")
        
        # Debug: Print response
        if self.debug:
            print(f"[BINANCE RESPONSE]")
            print(f"  Status: {status_code}")
            # Filter response for balance endpoint to show only important assets
            if isinstance(result, list) and len(result) > 0 and "asset" in result[0]:
                # Balance response - only show assets with balance > 0 or USDT
                filtered = [a for a in result if a.get("asset") == "USDT" or float(a.get("balance", 0)) > 0]
                print(f"  Data (filtered USDT + non-zero): {json.dumps(filtered, indent=2)}")
            else:
                print(f"  Data: {json.dumps(result, indent=2)[:2000]}")
            print(f"{'='*60}\n")
        
        # Check for error - but code 200 means success
        if isinstance(result, dict) and result.get("code") and result.get("code") != 200:
            raise Exception(f"Binance API Error: {result.get('msg', 'Unknown error')} (code: {result.get('code')})")
        
        return result
    
    async def get_balance(self, currency: str = "USDT") -> Balance:
        """Get account balance"""
        result = await self._request("GET", "/fapi/v2/balance")
        
        for asset in result:
            if asset["asset"] == currency:
                return Balance(
                    currency=currency,
                    total=float(asset.get("balance", 0)),
                    available=float(asset.get("availableBalance", 0)),
                    frozen=float(asset.get("balance", 0)) - float(asset.get("availableBalance", 0)),
                    raw_data=asset
                )
        
        return Balance(currency=currency, total=0, available=0, frozen=0)
    
    async def get_position(self, symbol: str, force_rest: bool = False) -> Optional[Position]:
        """Get position for symbol (Check CACHE then REST)"""
        binance_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BINANCE)
        
        # 1. Check Cache (skip if force_rest=True)
        if not force_rest and self.ws_manager.connected and binance_symbol in self._position_cache:
            # We can optionally check if cache is stale here, e.g. > 10s
            # For now, rely on WS push
            return self._position_cache[binance_symbol]

        # 2. Fallback to REST
        result = await self._request("GET", "/fapi/v2/positionRisk", {"symbol": binance_symbol})
        
        for pos_data in result:
            if pos_data["symbol"] == binance_symbol:
                pos_amt = float(pos_data.get("positionAmt", 0))
                
                if pos_amt == 0:
                    continue
                
                side = Side.LONG if pos_amt > 0 else Side.SHORT
                
                return Position(
                    symbol=binance_symbol,
                    side=side,
                    size=abs(pos_amt),
                    entry_price=float(pos_data.get("entryPrice", 0)),
                    mark_price=float(pos_data.get("markPrice", 0)),
                    liquidation_price=float(pos_data.get("liquidationPrice", 0)),
                    unrealized_pnl=float(pos_data.get("unRealizedProfit", 0)),
                    leverage=int(pos_data.get("leverage", 1)),
                    status=PositionStatus.OPEN,
                    timestamp=datetime.now(timezone.utc),
                    raw_data=pos_data
                )
        
        return None
    
    async def get_all_positions(self) -> List[Position]:
        """Get all open positions"""
        result = await self._request("GET", "/fapi/v2/positionRisk")
        
        positions = []
        for pos_data in result:
            pos_amt = float(pos_data.get("positionAmt", 0))
            
            if pos_amt == 0:
                continue
            
            side = Side.LONG if pos_amt > 0 else Side.SHORT
            
            positions.append(Position(
                symbol=pos_data["symbol"],
                side=side,
                size=abs(pos_amt),
                entry_price=float(pos_data.get("entryPrice", 0)),
                mark_price=float(pos_data.get("markPrice", 0)),
                liquidation_price=float(pos_data.get("liquidationPrice", 0)),
                unrealized_pnl=float(pos_data.get("unRealizedProfit", 0)),
                leverage=int(pos_data.get("leverage", 1)),
                status=PositionStatus.OPEN,
                timestamp=datetime.now(timezone.utc),
                raw_data=pos_data
            ))
        
        return positions
    
    async def place_market_order(
        self,
        symbol: str,
        side: Side,
        size: float,
        reduce_only: bool = False
    ) -> Order:
        """Place a market order"""
        binance_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BINANCE)
        
        # Get current price to convert USDT size to asset size
        current_price = await self.get_mark_price(binance_symbol)
        asset_size = size / current_price if current_price else size
        
        # Format size based on symbol to avoid precision errors
        if "BTC" in binance_symbol:
            asset_size = round(asset_size, 3)
        elif "ETH" in binance_symbol:
            asset_size = round(asset_size, 2)
        elif asset_size < 1:
            asset_size = round(asset_size, 4)
        else:
            asset_size = self._round_quantity(binance_symbol, asset_size)
            
        params = {
            "symbol": binance_symbol,
            "side": "BUY" if side == Side.LONG else "SELL",
            "type": "MARKET",
            "quantity": asset_size,
        }
        
        if reduce_only:
            params["reduceOnly"] = "true"
        elif self.hedge_mode:
            # Set position side for hedge mode
            params["positionSide"] = "LONG" if side == Side.LONG else "SHORT"
        
        result = await self._request("POST", "/fapi/v1/order", params)
        
        return Order(
            order_id=str(result["orderId"]),
            symbol=binance_symbol,
            side=side,
            order_type=OrderType.MARKET,
            size=float(result.get("origQty", size)),
            price=None,
            filled_size=float(result.get("executedQty", 0)),
            avg_price=float(result.get("avgPrice", 0)),
            status=result.get("status", "NEW"),
            timestamp=datetime.fromtimestamp(result.get("updateTime", time.time() * 1000) / 1000, tz=timezone.utc),
            raw_data=result
        )
    
    async def close_position(self, symbol: str, aggressive: bool = False) -> Order:
        """Close position for symbol
        
        Args:
            symbol: Trading symbol
            aggressive: If True, uses aggressive limit order (5% off market) for guaranteed fill
        """
        binance_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BINANCE)
        
        position = await self.get_position(binance_symbol)
        
        if not position:
            raise Exception(f"No position found for {binance_symbol}")
        
        # Close by placing opposite order
        # In hedge mode, use positionSide (do NOT use reduceOnly)
        # The side must be opposite: SELL to close LONG, BUY to close SHORT
        close_side = "SELL" if position.side == Side.LONG else "BUY"
        
        if aggressive:
            # AGGRESSIVE MODE: Use limit order with extreme price
            current_price = position.mark_price
            
            # For SELL (closing LONG): set price 5% below market
            # For BUY (closing SHORT): set price 5% above market
            if position.side == Side.LONG:
                aggressive_price = current_price * 0.95  # SELL 5% below
            else:
                aggressive_price = current_price * 1.05  # BUY 5% above
            
            # Round to Binance's tick size (usually 0.1 or 0.01)
            if aggressive_price > 1000:
                aggressive_price = round(aggressive_price, 1)
            elif aggressive_price > 100:
                aggressive_price = round(aggressive_price, 2)
            else:
                aggressive_price = round(aggressive_price, 3)
            
            params = {
                "symbol": binance_symbol,
                "side": close_side,
                "type": "LIMIT",
                "timeInForce": "IOC",  # Immediate Or Cancel
                "price": aggressive_price,
                "quantity": position.size,
            }
            if self.hedge_mode:
                params["positionSide"] = "LONG" if position.side == Side.LONG else "SHORT"
            else:
                params["reduceOnly"] = "true"
        else:
            # STANDARD MODE: Market order
            params = {
                "symbol": binance_symbol,
                "side": close_side,
                "type": "MARKET",
                "quantity": position.size,
            }
            if self.hedge_mode:
                params["positionSide"] = "LONG" if position.side == Side.LONG else "SHORT"
            else:
                params["reduceOnly"] = "true"
        
        result = await self._request("POST", "/fapi/v1/order", params)
        
        return Order(
            order_id=str(result["orderId"]),
            symbol=binance_symbol,
            side=Side.SHORT if position.side == Side.LONG else Side.LONG,
            order_type=OrderType.MARKET,
            size=position.size,
            price=None,
            filled_size=float(result.get("executedQty", 0)),
            avg_price=float(result.get("avgPrice", 0)),
            status=result.get("status", "NEW"),
            timestamp=datetime.fromtimestamp(result.get("updateTime", time.time() * 1000) / 1000, tz=timezone.utc),
            raw_data=result
        )
    
    async def close_position_partial(self, symbol: str, size: float) -> Order:
        """Close partial position with specific size"""
        binance_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BINANCE)
        
        position = await self.get_position(binance_symbol)
        
        if not position:
            raise Exception(f"No position found for {binance_symbol}")
        
        # Round size to appropriate precision
        size = self._round_quantity(binance_symbol, size)
        
        close_side = "SELL" if position.side == Side.LONG else "BUY"
        
        params = {
            "symbol": binance_symbol,
            "side": close_side,
            "type": "MARKET",
            "quantity": size,
        }
        if self.hedge_mode:
            params["positionSide"] = "LONG" if position.side == Side.LONG else "SHORT"
        else:
            params["reduceOnly"] = "true"
        
        result = await self._request("POST", "/fapi/v1/order", params)
        
        return Order(
            order_id=str(result["orderId"]),
            symbol=binance_symbol,
            side=Side.SHORT if position.side == Side.LONG else Side.LONG,
            order_type=OrderType.MARKET,
            size=size,
            price=None,
            filled_size=float(result.get("executedQty", 0)),
            avg_price=float(result.get("avgPrice", 0)),
            status=result.get("status", "NEW"),
            timestamp=datetime.fromtimestamp(result.get("updateTime", time.time() * 1000) / 1000, tz=timezone.utc),
            raw_data=result
        )
    
    def _round_quantity(self, symbol: str, quantity: float) -> float:
        """Round quantity to appropriate precision for symbol"""
        # Most symbols use 3 decimal places, some use more
        # This is a simplified approach
        if quantity >= 1000:
            return round(quantity, 0)
        elif quantity >= 100:
            return round(quantity, 1)
        elif quantity >= 10:
            return round(quantity, 2)
        elif quantity >= 1:
            return round(quantity, 3)
        else:
            return round(quantity, 4)
    
    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for symbol"""
        binance_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BINANCE)
        
        try:
            await self._request("POST", "/fapi/v1/leverage", {
                "symbol": binance_symbol,
                "leverage": leverage
            })
            return True
        except Exception as e:
            print(f"Error setting leverage: {e}")
            return False
    
    async def _get_funding_info_cached(self) -> list:
        """Get fundingInfo with 5-minute cache to avoid rate limits"""
        now = time.time()
        if self._funding_info_cache and (now - self._funding_info_cache_time) < 300:
            return self._funding_info_cache
        
        try:
            result = await self._request("GET", "/fapi/v1/fundingInfo", {}, signed=False)
            if isinstance(result, list):
                self._funding_info_cache = result
                self._funding_info_cache_time = now
                return result
        except Exception as e:
            logger.warning(f"Binance failed to fetch funding info: {e}")
        
        return self._funding_info_cache or []
    
    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """Get current funding rate with dynamic interval"""
        binance_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BINANCE)
        
        # Fetch premiumIndex, use cached fundingInfo
        result = await self._request("GET", "/fapi/v1/premiumIndex", {"symbol": binance_symbol}, signed=False)
        funding_info = await self._get_funding_info_cached()
        
        # Look up dynamic interval
        funding_interval_hours = 8  # fallback
        for item in funding_info:
            if item.get("symbol") == binance_symbol:
                funding_interval_hours = item.get("fundingIntervalHours", 8)
                break
        
        next_funding_time = datetime.fromtimestamp(result.get("nextFundingTime", 0) / 1000, tz=timezone.utc)
        
        return FundingRate(
            symbol=binance_symbol,
            funding_rate=float(result.get("lastFundingRate", 0)),
            next_funding_time=next_funding_time,
            estimated_rate=float(result.get("interestRate", 0)),
            funding_interval_hours=funding_interval_hours,
            raw_data=result
        )
    
    async def get_all_funding_rates(self) -> List[FundingRate]:
        """Get all funding rates with dynamic intervals"""
        result = await self._request("GET", "/fapi/v1/premiumIndex", {}, signed=False)
        funding_info = await self._get_funding_info_cached()
        
        # Build interval lookup map
        interval_map = {}
        for item in funding_info:
            sym = item.get("symbol")
            if sym:
                interval_map[sym] = item.get("fundingIntervalHours", 8)
        
        funding_rates = []
        for item in result:
            symbol = item.get("symbol", "")
            # Only include USDT perpetual contracts
            if symbol.endswith("USDT"):
                funding_interval_hours = interval_map.get(symbol, 8)
                
                funding_rates.append(FundingRate(
                    symbol=symbol,
                    funding_rate=float(item.get("lastFundingRate", 0)),
                    next_funding_time=datetime.fromtimestamp(item.get("nextFundingTime", 0) / 1000, tz=timezone.utc),
                    estimated_rate=float(item.get("interestRate", 0)),
                    funding_interval_hours=funding_interval_hours,
                    raw_data=item
                ))
        
        return funding_rates
    
    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """Get ticker data"""
        binance_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BINANCE)
        
        result = await self._request("GET", "/fapi/v1/ticker/24hr", {"symbol": binance_symbol}, signed=False)
        
        return {
            "symbol": binance_symbol,
            "last": float(result.get("lastPrice", 0)),
            "bid": float(result.get("bidPrice", 0)),
            "ask": float(result.get("askPrice", 0)),
            "volume": float(result.get("volume", 0)),
            "raw": result
        }
    
    async def set_position_mode(self, hedge_mode: bool = True) -> bool:
        """Set position mode (hedge or one-way)"""
        try:
            await self._request("POST", "/fapi/v1/positionSide/dual", {
                "dualSidePosition": "true" if hedge_mode else "false"
            })
            self.hedge_mode = hedge_mode
            return True
        except Exception as e:
            # Already in the requested mode
            if "No need to change position side" in str(e) or "-4059" in str(e):
                self.hedge_mode = hedge_mode
                return True
            # Can't change because existing positions - query actual mode
            try:
                result = await self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
                self.hedge_mode = result.get("dualSidePosition", False)
            except:
                self.hedge_mode = False  # Safe default for Binance
            return False
    
    async def get_income_history(self, income_type: str = "FUNDING_FEE", limit: int = 100, symbol: Optional[str] = None) -> List[Dict]:
        """Get income history (funding fees, etc.)
        
        Args:
            income_type: Type of income (FUNDING_FEE, REALIZED_PNL, etc.)
            limit: Maximum number of records
            symbol: Optional symbol filter (applied client-side)
        """
        params = {
            "incomeType": income_type,
            "limit": limit
        }
        
        # Binance API doesn't support symbol filter, we filter client-side
        result = await self._request("GET", "/fapi/v1/income", params)
        
        # If symbol filter provided, filter the results
        if symbol:
            binance_symbol = symbol if "USDT" in symbol and "/" not in symbol else symbol.replace('/', '')
            result = [item for item in result if item.get('symbol') == binance_symbol]
        
        return result
    
    async def get_recent_orders(self, symbol: str, limit: int = 10) -> List[Dict]:
        """Get recent orders for a symbol
        
        Args:
            symbol: Trading pair symbol
            limit: Maximum number of orders to return
            
        Returns:
            List of recent orders with status, side, time, etc.
        """
        params = {
            "symbol": symbol,
            "limit": limit
        }
        
        result = await self._request("GET", "/fapi/v1/allOrders", params, signed=True)
        return result
    
    async def get_closed_pnl(self, symbol: str, since: Optional[int] = None) -> Dict[str, Any]:
        """Get realized PnL, commission fees, and funding fees for a closed position"""
        binance_symbol = get_exchange_symbol(symbol, Exchange.BINANCE)
        
        params = {"symbol": binance_symbol}
        if since:
            params["startTime"] = since
            
        try:
            # 1. Get Trades for Realized PnL and Commission
            trades = await self._request("GET", "/fapi/v1/userTrades", params, signed=True)
            
            realized_pnl = 0.0
            commission = 0.0
            
            for trade in trades:
                realized_pnl += float(trade.get("realizedPnl", 0))
                commission += float(trade.get("commission", 0))
                
            # 2. Get Income history for Funding Fees
            income_params = {"symbol": binance_symbol, "incomeType": "FUNDING_FEE"}
            if since:
                income_params["startTime"] = since
                
            incomes = await self._request("GET", "/fapi/v1/income", income_params, signed=True)
            
            funding_fee = 0.0
            for inc in incomes:
                funding_fee += float(inc.get("income", 0))
                
            net_pnl = realized_pnl - commission + funding_fee
            
            return {
                "realized_pnl": realized_pnl,
                "commission": commission,
                "funding_fee": funding_fee,
                "net_pnl": net_pnl
            }
        except Exception as e:
            logger.error(f"Error getting Binance PnL: {e}")
            return {
                "realized_pnl": 0.0,
                "commission": 0.0,
                "funding_fee": 0.0,
                "net_pnl": 0.0
            }
    
    def get_exchange_name(self) -> str:
        """Get exchange name"""
        return "Binance"

"""
BingX Futures API Client
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

from config.settings import BingXConfig
from config.constants import Side, OrderType, PositionStatus, Exchange, get_exchange_symbol
from .base import BaseExchangeClient, Position, Order, FundingRate, Balance

logger = logging.getLogger(__name__)


class BingXClient(BaseExchangeClient):
    """BingX Futures API Client"""
    
    
    def __init__(self, config: 'BingXConfig', debug: bool = False):
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
        self._ticker_cache: Dict[str, Dict] = {}
        self._position_cache: Dict[str, Position] = {}
        self._order_cache: Dict[str, Order] = {}
        
    def _get_timestamp(self) -> int:
        """Get current timestamp in milliseconds"""
        return int(time.time() * 1000)
    
    def _sign(self, params: Dict[str, Any]) -> str:
        """Generate signature for API request"""
        # BingX signatures are computed over the URL-encoded query string.
        # Be defensive: never include an existing `signature` field
        params_to_sign = {k: v for k, v in params.items() if k != "signature"}

        # Convert all values to strings and sort parameters alphabetically
        str_params = {k: str(v) for k, v in params_to_sign.items()}
        sorted_params = sorted(str_params.items())

        query_string = urlencode(sorted_params)
        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()
        return signature
    
    def _get_headers(self) -> Dict[str, str]:
        """Get headers for API request"""
        return {
            "X-BX-APIKEY": self.api_key,
            "Content-Type": "application/x-www-form-urlencoded"
        }
    
    async def connect(self) -> bool:
        """Connect to BingX API"""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        
        try:
            balance = await self.get_balance()
            
            # Start User Stream (ListenKey)
            await self.start_user_stream()
            
            return balance is not None
        except Exception as e:
            print(f"BingX connection error: {e}")
            if self._session:
                await self._session.close()
                self._session = None
            return False
            
    async def disconnect(self):
        """Disconnect from BingX API"""
        if self._listen_key_timer:
            self._listen_key_timer.cancel()
            
        await self.ws_manager.disconnect()
        
        if self._session:
            await self._session.close()
            # Give time for the underlying connections to close
            import asyncio
            await asyncio.sleep(0.25)
            self._session = None
            
    async def start_user_stream(self):
        """Start User Data Stream"""
        try:
            # Get Listen Key
            res = await self._request("POST", "/openApi/swap/v2/user/listenKey")
            if res.get("code") == 0:
                self._listen_key = res["data"]["listenKey"]
                logger.info(f"BingX ListenKey: {self._listen_key}")
                
                # BingX User Stream: append listenKey to URL
                self.ws_manager.url = f"{self.config.ws_url}?listenKey={self._listen_key}"
                
                await self.ws_manager.connect()
                
                # Start Heartbeat & Keepalive
                self._schedule_listen_key_keepalive()
                self._start_heartbeat_loop()
            else:
                logger.error(f"BingX ListenKey failed: {res}")
                
        except Exception as e:
            logger.error(f"Failed to start user stream: {e}")

    def _schedule_listen_key_keepalive(self):
        # BingX ListenKey valid for 60 mins, refresh every 30 mins
        async def keepalive():
            while True:
                await asyncio.sleep(1800)
                try:
                    await self._request("PUT", "/openApi/swap/v2/user/listenKey", {"listenKey": self._listen_key})
                    logger.debug("BingX ListenKey refreshed")
                except Exception as e:
                    logger.error(f"BingX ListenKey refresh failed: {e}")
        self._listen_key_timer = asyncio.create_task(keepalive())

    def _start_heartbeat_loop(self):
        # BingX requires Ping "{"ping": <timestamp>}" every 30s
        async def heartbeat():
            while True:
                await asyncio.sleep(30)
                try:
                    ts = int(time.time() * 1000)
                    await self.ws_manager.send_json({"ping": ts})
                except Exception as e:
                    pass # Ignore send errors, manager handles reconnect
        
        asyncio.create_task(heartbeat())

    async def subscribe_depth(self, symbol: str):
        """Subscribe to depth for symbol"""
        bingx_symbol = symbol if "-" in symbol else get_exchange_symbol(symbol, Exchange.BINGX)
        # BingX depth topic: "push.depth"
        # Payload: {"id": "...", "reqType": "sub", "dataType": f"{symbol}@depth10"}
        
        payload = {
            "id": str(int(time.time() * 1000)),
            "reqType": "sub",
            "dataType": f"{bingx_symbol}@depth5" # depth5 is enough for top of book
        }
        await self.ws_manager.subscribe(payload)
        logger.info(f"Subscribed to {bingx_symbol}@depth5")

    def _handle_ws_message(self, msg: Dict):
        """Process incoming WS messages"""
        # 1. Heartbeat Pong (ignore)
        if "pong" in msg:
            return
            
        # 2. Ping (Respond with Pong? Docs say server pushes Pong, maybe we need to pong back if server pings?)
        if "ping" in msg:
            asyncio.create_task(self.ws_manager.send_json({"pong": msg["ping"]}))
            return

        # 3. Data Format: {"code": 0, "dataType": "...", "data": ...}
        if msg.get("code") != 0:
            return
            
        data_type = msg.get("dataType", "")
        data = msg.get("data")
        
        if not data:
            return

        # Market Data (Depth)
        if "@depth" in data_type:
            # {"asks": [["price", "qty"]...], "bids": [...]}
            # DataType format: "BTC-USDT@depth5"
            symbol = data_type.split("@")[0]
            
            # Simple conversion to float
            bids = [[float(p), float(q)] for p, q in data.get("bids", [])]
            asks = [[float(p), float(q)] for p, q in data.get("asks", [])]
            
            if bids and asks:
                self._ticker_cache[symbol] = {
                    "bid": bids[0][0],
                    "bid_qty": bids[0][1],
                    "ask": asks[0][0],
                    "ask_qty": asks[0][1],
                    "time": time.time(),
                    "full_depth": {"bids": bids, "asks": asks} # Store full depth to use in get_order_book
                }

        # User Data (Order/Position)
        # Channel names usually: "listenKey" related events
        # BingX User Stream event types: "ORDER", "ACCOUNT_UPDATE" (check specific structure)
        # Structure: {"e": "ORDER", "E": 123456, "o": {...}}
        event_type = data.get("e")
        
        if event_type == "ACCOUNT_UPDATE":
             # Position Update
             # "a": {"B": [...], "P": [...]}
             update_data = data.get("a", {})
             for p in update_data.get("P", []):
                 symbol = p.get("s")
                 amt = float(p.get("pa", 0))
                 entry = float(p.get("ep", 0))
                 upnl = float(p.get("up", 0))
                 
                 side = Side.LONG if amt > 0 else Side.SHORT
                 if amt == 0:
                     if symbol in self._position_cache:
                         del self._position_cache[symbol]
                 else:
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
                     logger.info(f"BingX WS Pos: {symbol} {amt}")

        elif event_type == "ORDER":
             # Order Update
             o = data.get("o", {})
             oid = str(o.get("i"))
             status = o.get("X")
             logger.info(f"BingX WS Order {oid}: {status}")
    
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        signed: bool = True
    ) -> Any:
        """Make API request"""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        
        url = f"{self.base_url}{endpoint}"
        params = params or {}
        signature = None
        
        if signed:
            # Add timestamp
            params["timestamp"] = self._get_timestamp()

            # Generate signature from params (without signature)
            signature = self._sign(params)

            # Build parameter string (same order as signed string)
            params_without_sig = {k: v for k, v in params.items() if k != "signature"}
            query_string = urlencode(sorted((k, str(v)) for k, v in params_without_sig.items()))
            # Append signature at the end
            param_string = f"{query_string}&signature={signature}"
        else:
            # For unsigned requests, just encode all params
            param_string = urlencode(sorted((k, str(v)) for k, v in params.items()))
        
        headers = self._get_headers()
        
        # BingX uses different methods for different endpoints:
        # - GET requests: parameters in query string
        # - POST requests: parameters in query string (NOT form body or JSON)
        # This is consistent across their API
        full_url = f"{url}?{param_string}"
        
        # Debug: Print request details
        if self.debug:
            print(f"\n{'='*60}")
            print(f"[BINGX REQUEST]")
            print(f"  Method: {method}")
            print(f"  Base URL: {url}")
            print(f"  Param String: {param_string}")
            print(f"  Full URL: {full_url}")

            if signed and signature:
                params_without_sig = {k: v for k, v in params.items() if k != "signature"}
                string_to_sign = urlencode(sorted((k, str(v)) for k, v in params_without_sig.items()))
                print(f"  StringToSign: {string_to_sign}")
                print(f"  Params: {json.dumps(params_without_sig, indent=2)}")
                print(f"  Signature: {signature[:16]}...")
            print(f"  Headers: X-BX-APIKEY: {self.api_key[:8]}...")
        
        # Send request with full URL (no params argument to avoid re-encoding)
        # Add timeout to prevent hanging
        req_timeout = aiohttp.ClientTimeout(total=15.0)
        try:
            async with self._session.request(method, full_url, headers=headers, timeout=req_timeout) as response:
                result = await response.json()
                
                # Debug: Print response
                if self.debug:
                    print(f"[BINGX RESPONSE]")
                    print(f"  Status: {response.status}")
                    print(f"  Data: {json.dumps(result, indent=2)[:1000]}")
                    print(f"{'='*60}\n")
        except asyncio.TimeoutError:
            raise Exception(f"BingX API request timeout after 15s")
        except Exception as e:
            print(f"Request error: {e}")
            raise
        
        if isinstance(result, dict) and result.get("code") and result.get("code") != 0:
            raise Exception(f"BingX API Error: {result.get('msg', 'Unknown error')} (code: {result.get('code')})")
        
        return result

    
    async def get_balance(self, currency: str = "USDT") -> Balance:
        """Get account balance"""
        result = await self._request("GET", "/openApi/swap/v2/user/balance")
        
        if result.get("code") == 0 and result.get("data"):
            data = result["data"].get("balance", {})
            return Balance(
                currency=currency,
                total=float(data.get("balance", 0)),
                available=float(data.get("availableMargin", 0)),
                frozen=float(data.get("usedMargin", 0)),
                raw_data=data
            )
        
        return Balance(currency=currency, total=0, available=0, frozen=0)
    
    async def get_position(self, symbol: str, force_rest: bool = False) -> Optional[Position]:
        """Get position for symbol (Check CACHE then REST)"""
        bingx_symbol = symbol if "-" in symbol else get_exchange_symbol(symbol, Exchange.BINGX)
        
        # 1. Check Cache (skip if force_rest=True)
        if not force_rest and self.ws_manager.connected and bingx_symbol in self._position_cache:
            return self._position_cache[bingx_symbol]

        # 2. Fallback to REST
        result = await self._request("GET", "/openApi/swap/v2/user/positions", {"symbol": bingx_symbol})
        
        if result.get("code") == 0 and result.get("data"):
            for pos_data in result["data"]:
                if pos_data.get("symbol") == bingx_symbol:
                    pos_amt = float(pos_data.get("positionAmt", 0))
                    
                    if pos_amt == 0:
                        continue
                    
                    pos_side = pos_data.get("positionSide", "")
                    if pos_side == "LONG":
                        side = Side.LONG
                    elif pos_side == "SHORT":
                        side = Side.SHORT
                    else:
                        side = Side.LONG if pos_amt > 0 else Side.SHORT
                    
                    return Position(
                        symbol=bingx_symbol,
                        side=side,
                        size=abs(pos_amt),
                        entry_price=float(pos_data.get("avgPrice", 0)),
                        mark_price=float(pos_data.get("markPrice", 0)),
                        liquidation_price=float(pos_data.get("liquidationPrice", 0)) if pos_data.get("liquidationPrice") else 0,
                        unrealized_pnl=float(pos_data.get("unrealizedProfit", 0)),
                        leverage=int(pos_data.get("leverage", 1)),
                        status=PositionStatus.OPEN,
                        timestamp=datetime.now(timezone.utc),
                        raw_data=pos_data
                    )
        
        return None
    
    async def get_all_positions(self) -> List[Position]:
        """Get all open positions"""
        result = await self._request("GET", "/openApi/swap/v2/user/positions")
        
        positions = []
        if result.get("code") == 0 and result.get("data"):
            for pos_data in result["data"]:
                pos_amt = float(pos_data.get("positionAmt", 0))
                
                if pos_amt == 0:
                    continue
                
                # BingX returns positionSide: "LONG" or "SHORT" in hedge mode
                pos_side = pos_data.get("positionSide", "")
                if pos_side == "LONG":
                    side = Side.LONG
                elif pos_side == "SHORT":
                    side = Side.SHORT
                else:
                    # Fallback: use positionAmt sign (for one-way mode)
                    side = Side.LONG if pos_amt > 0 else Side.SHORT
                
                positions.append(Position(
                    symbol=pos_data["symbol"],
                    side=side,
                    size=abs(pos_amt),
                    entry_price=float(pos_data.get("avgPrice", 0)),
                    mark_price=float(pos_data.get("markPrice", 0)),
                    liquidation_price=float(pos_data.get("liquidationPrice", 0)) if pos_data.get("liquidationPrice") else 0,
                    unrealized_pnl=float(pos_data.get("unrealizedProfit", 0)),
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
        bingx_symbol = symbol if "-" in symbol else get_exchange_symbol(symbol, Exchange.BINGX)
        
        # BingX expects quantity as a string to avoid floating point precision issues
        params = {
            "symbol": bingx_symbol,
            "side": "BUY" if side == Side.LONG else "SELL",
            "type": "MARKET",
            "quantity": str(size),
            "positionSide": "LONG" if side == Side.LONG else "SHORT"
        }
        
        if reduce_only:
            # For closing, reverse the side
            params["positionSide"] = "SHORT" if side == Side.LONG else "LONG"
        
        result = await self._request("POST", "/openApi/swap/v2/trade/order", params)
        
        if result.get("code") != 0:
            raise Exception(f"Order failed: {result.get('msg')}")
        
        order_data = result.get("data", {}).get("order", {})
        
        return Order(
            order_id=str(order_data.get("orderId", "")),
            symbol=bingx_symbol,
            side=side,
            order_type=OrderType.MARKET,
            size=float(order_data.get("origQty", size)),
            price=None,
            filled_size=float(order_data.get("executedQty", 0)),
            avg_price=float(order_data.get("avgPrice", 0)),
            status=order_data.get("status", "NEW"),
            timestamp=datetime.now(timezone.utc),
            raw_data=order_data
        )
    
    async def close_position(self, symbol: str, aggressive: bool = False) -> Order:
        """Close position for symbol
        
        Args:
            symbol: Trading symbol
            aggressive: If True, uses aggressive limit order to ensure fill (faster execution)
                       If False, uses standard market order with IOC
        """
        bingx_symbol = symbol if "-" in symbol else get_exchange_symbol(symbol, Exchange.BINGX)
        
        position = await self.get_position(bingx_symbol)
        
        if not position:
            raise Exception(f"No position found for {bingx_symbol}")
        
        # Close by placing opposite order with closePosition flag
        # For BingX: to close a LONG position, we SELL with positionSide=LONG
        # For closing, the side is opposite but positionSide stays the same
        close_side = "SELL" if position.side == Side.LONG else "BUY"
        
        if aggressive:
            # AGGRESSIVE MODE: Use limit order with extreme price to guarantee fill
            # This is the "nhồi lệnh" technique
            current_price = position.mark_price
            
            # For SELL (closing LONG): set price 5% below market (always fills)
            # For BUY (closing SHORT): set price 5% above market (always fills)
            if close_side == "SELL":
                aggressive_price = current_price * 0.95  # 5% below
            else:
                aggressive_price = current_price * 1.05  # 5% above
            
            # Round to appropriate decimal places (BingX typically uses 2-4 decimals)
            if aggressive_price > 1000:
                aggressive_price = round(aggressive_price, 1)
            elif aggressive_price > 100:
                aggressive_price = round(aggressive_price, 2)
            elif aggressive_price > 10:
                aggressive_price = round(aggressive_price, 3)
            else:
                aggressive_price = round(aggressive_price, 4)
            
            params = {
                "symbol": bingx_symbol,
                "side": close_side,
                "type": "LIMIT",  # Use LIMIT for aggressive close
                "price": str(aggressive_price),
                "quantity": str(position.size),
                "positionSide": "LONG" if position.side == Side.LONG else "SHORT",
                "closePosition": "true",
                "timeInForce": "IOC"  # Must fill immediately
            }
        else:
            # STANDARD MODE: Market order with IOC (default)
            params = {
                "symbol": bingx_symbol,
                "side": close_side,
                "type": "MARKET",
                "quantity": str(position.size),
                "positionSide": "LONG" if position.side == Side.LONG else "SHORT",
                "closePosition": "true",  # BingX parameter to close position
                "timeInForce": "IOC"  # Fill immediately or cancel (fastest execution)
            }
        
        result = await self._request("POST", "/openApi/swap/v2/trade/order", params)
        
        if result.get("code") != 0:
            raise Exception(f"Close order failed: {result.get('msg')}")
        
        order_data = result.get("data", {}).get("order", {})
        
        return Order(
            order_id=str(order_data.get("orderId", "")),
            symbol=bingx_symbol,
            side=Side.SHORT if position.side == Side.LONG else Side.LONG,
            order_type=OrderType.MARKET,
            size=position.size,
            price=None,
            filled_size=float(order_data.get("executedQty", 0)),
            avg_price=float(order_data.get("avgPrice", 0)),
            status=order_data.get("status", "NEW"),
            timestamp=datetime.now(timezone.utc),
            raw_data=order_data
        )
    
    async def close_position_partial(self, symbol: str, size: float) -> Order:
        """Close partial position with specific size"""
        bingx_symbol = symbol if "-" in symbol else get_exchange_symbol(symbol, Exchange.BINGX)
        
        position = await self.get_position(bingx_symbol)
        
        if not position:
            raise Exception(f"No position found for {bingx_symbol}")
        
        # Round size to appropriate precision
        size = self._round_quantity(size)
        
        close_side = "SELL" if position.side == Side.LONG else "BUY"
        
        params = {
            "symbol": bingx_symbol,
            "side": close_side,
            "type": "MARKET",
            "quantity": str(size),
            "positionSide": "LONG" if position.side == Side.LONG else "SHORT",
            "timeInForce": "IOC"
        }
        
        result = await self._request("POST", "/openApi/swap/v2/trade/order", params)
        
        if result.get("code") != 0:
            raise Exception(f"Close order failed: {result.get('msg')}")
        
        order_data = result.get("data", {}).get("order", {})
        
        return Order(
            order_id=str(order_data.get("orderId", "")),
            symbol=bingx_symbol,
            side=Side.SHORT if position.side == Side.LONG else Side.LONG,
            order_type=OrderType.MARKET,
            size=size,
            price=None,
            filled_size=float(order_data.get("executedQty", 0)),
            avg_price=float(order_data.get("avgPrice", 0)),
            status=order_data.get("status", "NEW"),
            timestamp=datetime.now(timezone.utc),
            raw_data=order_data
        )
    
    def _round_quantity(self, quantity: float) -> float:
        """Round quantity to appropriate precision"""
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
        bingx_symbol = symbol if "-" in symbol else get_exchange_symbol(symbol, Exchange.BINGX)
        
        # Set for both long and short
        for pos_side in ["LONG", "SHORT"]:
            try:
                # BingX expects leverage as string
                await self._request("POST", "/openApi/swap/v2/trade/leverage", {
                    "symbol": bingx_symbol,
                    "leverage": str(leverage),
                    "side": pos_side
                })
            except Exception as e:
                print(f"Error setting leverage for {pos_side}: {e}")
                return False
        
        return True
    
    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """Get current funding rate with interval from history"""
        bingx_symbol = symbol if "-" in symbol else get_exchange_symbol(symbol, Exchange.BINGX)
        
        # Fetch premiumIndex and funding rate history concurrently
        premium_task = asyncio.create_task(
            self._request("GET", "/openApi/swap/v2/quote/premiumIndex", 
                         {"symbol": bingx_symbol}, signed=False))
        history_task = asyncio.create_task(
            self._request("GET", "/openApi/swap/v2/quote/fundingRate",
                         {"symbol": bingx_symbol}, signed=False))
        
        result = await premium_task
        
        # Determine interval from history
        funding_interval_hours = 8  # fallback
        try:
            history_result = await history_task
            history_items = history_result.get("data", [])
            if isinstance(history_items, list) and len(history_items) >= 2:
                t1 = int(history_items[0].get("fundingTime", 0))
                t2 = int(history_items[1].get("fundingTime", 0))
                if t1 and t2:
                    diff_hours = abs(t1 - t2) / 3600000
                    # Round to nearest standard interval
                    if diff_hours <= 1.5:
                        funding_interval_hours = 1
                    elif diff_hours <= 3:
                        funding_interval_hours = 2
                    elif diff_hours <= 6:
                        funding_interval_hours = 4
                    else:
                        funding_interval_hours = 8
        except Exception as e:
            logger.debug(f"BingX funding history unavailable for {bingx_symbol}: {e}")
        
        if result.get("code") == 0 and result.get("data"):
            data = result["data"]
            next_funding_time_ms = int(data.get("nextFundingTime", 0))
            
            return FundingRate(
                symbol=bingx_symbol,
                funding_rate=float(data.get("lastFundingRate", 0)),
                next_funding_time=datetime.fromtimestamp(next_funding_time_ms / 1000, tz=timezone.utc) if next_funding_time_ms else datetime.now(timezone.utc),
                estimated_rate=float(data.get("estimatedSettlePrice", 0)) if data.get("estimatedSettlePrice") else None,
                funding_interval_hours=funding_interval_hours,
                raw_data=data
            )
        
        raise Exception(f"No funding rate data for {bingx_symbol}")
    
    async def get_mark_price(self, symbol: str) -> float:
        """Get current mark price"""
        bingx_symbol = symbol if "-" in symbol else get_exchange_symbol(symbol, Exchange.BINGX)
        
        result = await self._request("GET", "/openApi/swap/v2/quote/premiumIndex",
                                     {"symbol": bingx_symbol}, signed=False)
        
        if result.get("code") == 0 and result.get("data"):
            return float(result["data"].get("markPrice", 0))
        
        raise Exception(f"No mark price data for {bingx_symbol}")
    
    async def get_order_book(self, symbol: str, limit: int = 20) -> Dict[str, Any]:
        """Get order book (depth)"""
        bingx_symbol = symbol if "-" in symbol else get_exchange_symbol(symbol, Exchange.BINGX)
        
        # 1. OPTIMIZATION: Check Ticker Cache
        if bingx_symbol in self._ticker_cache:
            ticker = self._ticker_cache[bingx_symbol]
            if time.time() - ticker.get("time", 0) < 5.0 and "full_depth" in ticker:
                # Return the cached full depth
                # Just take needed limit
                full = ticker["full_depth"]
                return {
                    "bids": full["bids"][:limit],
                    "asks": full["asks"][:limit]
                }
        
        # 2. Fallback to REST
        result = await self._request("GET", "/openApi/swap/v2/quote/depth",
                                     {"symbol": bingx_symbol, "limit": limit}, signed=False)
        
        if result.get("code") == 0 and result.get("data"):
            data = result["data"]
            
            bids = []
            asks = []
            
            if "bids" in data:
                for b in data["bids"]:
                    if isinstance(b, dict):
                        bids.append([float(b.get("p", 0)), float(b.get("v", 0))])
                    elif isinstance(b, list) and len(b) >= 2:
                        bids.append([float(b[0]), float(b[1])])
            
            if "asks" in data:
                for a in data["asks"]:
                    if isinstance(a, dict):
                        asks.append([float(a.get("p", 0)), float(a.get("v", 0))])
                    elif isinstance(a, list) and len(a) >= 2:
                        asks.append([float(a[0]), float(a[1])])
            
            return {"bids": bids, "asks": asks}
        
        return {"bids": [], "asks": []}
    
    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """Get ticker data"""
        bingx_symbol = symbol if "-" in symbol else get_exchange_symbol(symbol, Exchange.BINGX)
        
        result = await self._request("GET", "/openApi/swap/v2/quote/ticker",
                                     {"symbol": bingx_symbol}, signed=False)
        
        if result.get("code") == 0 and result.get("data"):
            data = result["data"]
            return {
                "symbol": bingx_symbol,
                "last": float(data.get("lastPrice", 0)),
                "bid": float(data.get("bidPrice", 0)),
                "ask": float(data.get("askPrice", 0)),
                "volume": float(data.get("volume", 0)),
                "raw": data
            }
        
        raise Exception(f"No ticker data for {bingx_symbol}")
    
    async def set_position_mode(self, hedge_mode: bool = True) -> bool:
        """Set position mode (hedge or one-way)"""
        try:
            # Note: BingX uses v1 endpoint for position mode, not v2
            await self._request("POST", "/openApi/swap/v1/positionSide/dual", {
                "dualSidePosition": "true" if hedge_mode else "false"
            })
            return True
        except Exception as e:
            # Might fail if already in the requested mode
            if "No need to change" in str(e) or "already" in str(e).lower():
                return True
            print(f"Error setting position mode: {e}")
            return False
    
    async def get_income_history(self, symbol: Optional[str] = None, limit: int = 100, income_type: str = "FUNDING_FEE") -> List[Dict]:
        """Get income history (funding fees, realized PnL, trading fees, etc.)
        
        Args:
            symbol: BingX symbol (e.g., BTC-USDT), optional
            limit: Maximum number of records to return
            income_type: Type of income - "FUNDING_FEE", "REALIZED_PNL", or "COMMISSION"
            
        Returns:
            List of income records with keys: symbol, income, time, type
            
        BingX valid incomeTypes:
            TRANSFER, REALIZED_PNL, FUNDING_FEE, TRADING_FEE, 
            INSURANCE_CLEAR, TRIAL_FUND, ADL, SYSTEM_DEDUCTION, GTD_PRICE
        """
        # Map common names to BingX API names
        income_type_map = {
            "COMMISSION": "TRADING_FEE",  # Map COMMISSION to BingX's TRADING_FEE
            "FUNDING_FEE": "FUNDING_FEE",
            "REALIZED_PNL": "REALIZED_PNL"
        }
        bingx_income_type = income_type_map.get(income_type, income_type)
        
        params = {
            "incomeType": bingx_income_type,
            "limit": str(limit)
        }
        
        if symbol:
            bingx_symbol = symbol if "-" in symbol else get_exchange_symbol(symbol, Exchange.BINGX)
            params["symbol"] = bingx_symbol
        
        try:
            result = await self._request("GET", "/openApi/swap/v2/user/income", params)
            
            # Transform to common format
            income_list = []
            if result.get("code") == 0 and result.get("data"):
                # BingX returns data as a direct array, not nested in "incomes"
                data = result.get("data", [])
                if isinstance(data, list):
                    for item in data:
                        income_list.append({
                            "symbol": item.get("symbol", ""),
                            "income": float(item.get("income", 0)),
                            "time": int(item.get("time", 0)),  # Timestamp in milliseconds
                            "type": income_type
                        })
            
            return income_list
        except Exception as e:
            print(f"Error getting BingX income history: {e}")
            return []
    
    async def get_recent_orders(self, symbol: str, limit: int = 10) -> List[Dict]:
        """Get recent orders for a symbol
        
        Args:
            symbol: Trading pair symbol (e.g., BTC-USDT)
            limit: Maximum number of orders to return
            
        Returns:
            List of recent orders
        """
        bingx_symbol = symbol if "-" in symbol else get_exchange_symbol(symbol.replace("USDT", ""), Exchange.BINGX)
        
        params = {
            "symbol": bingx_symbol,
            "limit": str(limit)
        }
        
        try:
            result = await self._request("GET", "/openApi/swap/v2/trade/allOrders", params)
            if result.get("code") == 0:
                return result.get("data", {}).get("orders", [])
            return []
        except Exception as e:
            print(f"Error getting BingX order history: {e}")
            return []
            
    async def get_closed_pnl(self, symbol: str, since: Optional[int] = None) -> Dict[str, Any]:
        """Get realized PnL, commission fees, and funding fees for a closed position"""
        bingx_symbol = symbol if "-" in symbol else get_exchange_symbol(symbol, Exchange.BINGX)
        
        realized_pnl = 0.0
        commission = 0.0
        funding_fee = 0.0
        
        params = {
            "symbol": bingx_symbol,
            "limit": "100"
        }
        if since:
            params["startTime"] = str(since)
            
        try:
            # 1. Realized PnL
            r_params = params.copy()
            r_params["incomeType"] = "REALIZED_PNL"
            
            pnl_res = await self._request("GET", "/openApi/swap/v2/user/income", r_params)
            if pnl_res.get("code") == 0 and pnl_res.get("data"):
                for item in pnl_res.get("data", []):
                    realized_pnl += float(item.get("income", 0))
            
            # 2. Commission (TRADING_FEE is usually negative)
            c_params = params.copy()
            c_params["incomeType"] = "TRADING_FEE"
            
            c_res = await self._request("GET", "/openApi/swap/v2/user/income", c_params)
            if c_res.get("code") == 0 and c_res.get("data"):
                for item in c_res.get("data", []):
                    commission += abs(float(item.get("income", 0)))
            
            # 3. Funding Fee
            f_params = params.copy()
            f_params["incomeType"] = "FUNDING_FEE"
            
            f_res = await self._request("GET", "/openApi/swap/v2/user/income", f_params)
            if f_res.get("code") == 0 and f_res.get("data"):
                for item in f_res.get("data", []):
                    funding_fee += float(item.get("income", 0))
                    
            net_pnl = realized_pnl - commission + funding_fee
            
            return {
                "realized_pnl": realized_pnl,
                "commission": commission,
                "funding_fee": funding_fee,
                "net_pnl": net_pnl
            }
        except Exception as e:
            print(f"Error getting BingX PnL: {e}")
            return {
                "realized_pnl": 0.0,
                "commission": 0.0,
                "funding_fee": 0.0,
                "net_pnl": 0.0
            }
    
    def get_exchange_name(self) -> str:
        """Get exchange name"""
        return "BingX"

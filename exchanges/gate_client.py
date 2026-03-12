"""
Gate.io Futures API Client
"""
import hmac
import hashlib
import time
import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
import aiohttp

from config.settings import GateConfig
from config.constants import Side, OrderType, PositionStatus, Exchange, get_exchange_symbol
from .base import BaseExchangeClient, Position, Order, FundingRate, Balance

logger = logging.getLogger(__name__)


class GateClient(BaseExchangeClient):
    """Gate.io Futures API Client"""
    
    def __init__(self, config: 'GateConfig', debug: bool = False):
        super().__init__(config.api_key, config.secret_key)
        self.config = config
        self.base_url = config.base_url
        self._session: Optional[aiohttp.ClientSession] = None
        self.debug = debug
        
        # Caches
        self._ticker_cache: Dict[str, Dict] = {}
        self._position_cache: Dict[str, Position] = {}
        
    def _get_timestamp(self) -> str:
        """Get current timestamp in seconds as string"""
        return str(int(time.time()))
    
    def _sign(self, method: str, url: str, query_string: str = "", body: str = "") -> Dict[str, str]:
        """Generate signature for API request
        
        Gate.io signature format:
        sign_string = method + "\n" + url + "\n" + query_string + "\n" + hashed_body + "\n" + timestamp
        signature = HMAC-SHA512(sign_string, secret_key)
        """
        t = self._get_timestamp()
        
        # Hash the body (empty string if no body)
        hashed_body = hashlib.sha512(body.encode('utf-8')).hexdigest()
        
        # Create sign string
        sign_string = f"{method}\n{url}\n{query_string}\n{hashed_body}\n{t}"
        
        # Generate signature
        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            sign_string.encode('utf-8'),
            hashlib.sha512
        ).hexdigest()
        
        headers = {
            "KEY": self.api_key,
            "Timestamp": t,
            "SIGN": signature,
            "Content-Type": "application/json"
        }
        
        return headers
    
    async def connect(self) -> bool:
        """Connect to Gate.io API"""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        
        try:
            balance = await self.get_balance()
            return balance is not None
        except Exception as e:
            print(f"Gate.io connection error: {e}")
            if self._session:
                await self._session.close()
                self._session = None
            return False
    
    async def disconnect(self):
        """Disconnect from Gate.io API"""
        if self._session:
            await self._session.close()
            await asyncio.sleep(0.25)
            self._session = None
    
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        body: Optional[Dict] = None,
        signed: bool = True
    ) -> Any:
        """Make API request to Gate.io"""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        
        # Build URL and query string
        url_path = endpoint
        query_string = ""
        
        if params:
            query_string = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        
        full_url = f"{self.base_url}{endpoint}"
        if query_string:
            full_url = f"{full_url}?{query_string}"
        
        # Prepare body
        body_str = ""
        if body:
            body_str = json.dumps(body)
        
        # Get headers (signed or unsigned)
        if signed:
            headers = self._sign(method, url_path, query_string, body_str)
        else:
            headers = {"Content-Type": "application/json"}
        
        # Debug logging
        if self.debug:
            print(f"\n{'='*60}")
            print(f"[GATE REQUEST]")
            print(f"  Method: {method}")
            print(f"  URL: {full_url}")
            print(f"  Query: {query_string}")
            print(f"  Body: {body_str[:200] if body_str else 'None'}")
            if signed:
                print(f"  Headers: KEY={self.api_key[:8]}..., Timestamp={headers.get('Timestamp')}")
        
        # Make request
        req_timeout = aiohttp.ClientTimeout(total=15.0)
        try:
            if method == "GET":
                async with self._session.get(full_url, headers=headers, timeout=req_timeout) as response:
                    result = await response.json()
                    status = response.status
            elif method == "POST":
                async with self._session.post(full_url, headers=headers, data=body_str, timeout=req_timeout) as response:
                    result = await response.json()
                    status = response.status
            elif method == "DELETE":
                async with self._session.delete(full_url, headers=headers, timeout=req_timeout) as response:
                    result = await response.json()
                    status = response.status
            else:
                raise Exception(f"Unsupported method: {method}")
            
            if self.debug:
                print(f"[GATE RESPONSE]")
                print(f"  Status: {status}")
                print(f"  Data: {json.dumps(result, indent=2)[:1000] if isinstance(result, (dict, list)) else result}")
                print(f"{'='*60}\n")
            
            # Gate.io returns errors with 'label' field
            if isinstance(result, dict) and result.get("label"):
                raise Exception(f"Gate.io API Error: {result.get('message', result.get('label'))} (label: {result.get('label')})")
            
            return result
            
        except asyncio.TimeoutError:
            raise Exception(f"Gate.io API request timeout after 15s")
        except Exception as e:
            if "API Error" in str(e):
                raise
            print(f"Gate.io request error: {e}")
            raise
    
    async def get_balance(self, currency: str = "USDT") -> Balance:
        """Get account balance"""
        # Gate.io futures uses settle currency in path
        result = await self._request("GET", "/api/v4/futures/usdt/accounts")
        
        if result:
            return Balance(
                currency=currency,
                total=float(result.get("total", 0)),
                available=float(result.get("available", 0)),
                frozen=float(result.get("total", 0)) - float(result.get("available", 0)),
                raw_data=result
            )
        
        return Balance(currency=currency, total=0, available=0, frozen=0)
    
    async def get_position(self, symbol: str) -> Optional[Position]:
        """Get position for symbol"""
        gate_symbol = symbol if "_" in symbol else get_exchange_symbol(symbol, Exchange.GATE)
        
        try:
            result = await self._request("GET", f"/api/v4/futures/usdt/positions/{gate_symbol}")
            
            if result:
                size = float(result.get("size", 0))
                
                if size == 0:
                    return None
                
                # Gate.io: positive size = long, negative size = short
                side = Side.LONG if size > 0 else Side.SHORT
                
                return Position(
                    symbol=gate_symbol,
                    side=side,
                    size=abs(size),
                    entry_price=float(result.get("entry_price", 0)),
                    mark_price=float(result.get("mark_price", 0)),
                    liquidation_price=float(result.get("liq_price", 0)) if result.get("liq_price") else 0,
                    unrealized_pnl=float(result.get("unrealised_pnl", 0)),
                    leverage=int(result.get("leverage", 1)),
                    status=PositionStatus.OPEN,
                    timestamp=datetime.now(timezone.utc),
                    raw_data=result
                )
        except Exception as e:
            if "POSITION_NOT_FOUND" in str(e) or "position not found" in str(e).lower():
                return None
            raise
        
        return None
    
    async def get_all_positions(self) -> List[Position]:
        """Get all open positions"""
        result = await self._request("GET", "/api/v4/futures/usdt/positions")
        
        positions = []
        if result and isinstance(result, list):
            for pos_data in result:
                size = float(pos_data.get("size", 0))
                
                if size == 0:
                    continue
                
                side = Side.LONG if size > 0 else Side.SHORT
                
                positions.append(Position(
                    symbol=pos_data.get("contract", ""),
                    side=side,
                    size=abs(size),
                    entry_price=float(pos_data.get("entry_price", 0)),
                    mark_price=float(pos_data.get("mark_price", 0)),
                    liquidation_price=float(pos_data.get("liq_price", 0)) if pos_data.get("liq_price") else 0,
                    unrealized_pnl=float(pos_data.get("unrealised_pnl", 0)),
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
        """Place a market order
        
        Gate.io uses contract size, not USDT value.
        Positive size = long, negative size = short
        """
        gate_symbol = symbol if "_" in symbol else get_exchange_symbol(symbol, Exchange.GATE)
        
        # Gate.io: positive size for long, negative for short
        order_size = int(size) if side == Side.LONG else -int(size)
        
        body = {
            "contract": gate_symbol,
            "size": order_size,
            "price": "0",  # Market order uses price=0
            "tif": "ioc",  # Immediate or cancel for market order
            "reduce_only": reduce_only
        }
        
        result = await self._request("POST", "/api/v4/futures/usdt/orders", body=body)
        
        return Order(
            order_id=str(result.get("id", "")),
            symbol=gate_symbol,
            side=side,
            order_type=OrderType.MARKET,
            size=abs(float(result.get("size", size))),
            price=None,
            filled_size=abs(float(result.get("size", 0)) - float(result.get("left", 0))),
            avg_price=float(result.get("fill_price", 0)) if result.get("fill_price") else 0,
            status=result.get("status", "open"),
            timestamp=datetime.now(timezone.utc),
            raw_data=result
        )
    
    async def close_position(self, symbol: str, aggressive: bool = False) -> Order:
        """Close position for symbol
        
        Args:
            symbol: Trading symbol
            aggressive: If True, uses aggressive pricing for guaranteed fill
        """
        gate_symbol = symbol if "_" in symbol else get_exchange_symbol(symbol, Exchange.GATE)
        
        position = await self.get_position(gate_symbol)
        
        if not position:
            raise Exception(f"No position found for {gate_symbol}")
        
        # To close: place opposite order with reduce_only
        # Gate.io: if we're long (positive size), close with negative size
        close_size = -int(position.size) if position.side == Side.LONG else int(position.size)
        
        if aggressive:
            # Use limit order with aggressive price
            current_price = position.mark_price
            
            if position.side == Side.LONG:
                # Closing long = selling, set price below market
                aggressive_price = current_price * 0.95
            else:
                # Closing short = buying, set price above market
                aggressive_price = current_price * 1.05
            
            # Round price appropriately
            if aggressive_price > 1000:
                aggressive_price = round(aggressive_price, 1)
            elif aggressive_price > 100:
                aggressive_price = round(aggressive_price, 2)
            else:
                aggressive_price = round(aggressive_price, 4)
            
            body = {
                "contract": gate_symbol,
                "size": close_size,
                "price": str(aggressive_price),
                "tif": "ioc",
                "reduce_only": True
            }
        else:
            # Market order
            body = {
                "contract": gate_symbol,
                "size": close_size,
                "price": "0",
                "tif": "ioc",
                "reduce_only": True
            }
        
        result = await self._request("POST", "/api/v4/futures/usdt/orders", body=body)
        
        return Order(
            order_id=str(result.get("id", "")),
            symbol=gate_symbol,
            side=Side.SHORT if position.side == Side.LONG else Side.LONG,
            order_type=OrderType.MARKET,
            size=position.size,
            price=None,
            filled_size=abs(float(result.get("size", 0)) - float(result.get("left", 0))),
            avg_price=float(result.get("fill_price", 0)) if result.get("fill_price") else 0,
            status=result.get("status", "open"),
            timestamp=datetime.now(timezone.utc),
            raw_data=result
        )
    
    async def close_position_partial(self, symbol: str, size: float) -> Order:
        """Close partial position with specific size"""
        gate_symbol = symbol if "_" in symbol else get_exchange_symbol(symbol, Exchange.GATE)
        
        position = await self.get_position(gate_symbol)
        
        if not position:
            raise Exception(f"No position found for {gate_symbol}")
        
        # Close partial: opposite direction
        close_size = -int(size) if position.side == Side.LONG else int(size)
        
        body = {
            "contract": gate_symbol,
            "size": close_size,
            "price": "0",
            "tif": "ioc",
            "reduce_only": True
        }
        
        result = await self._request("POST", "/api/v4/futures/usdt/orders", body=body)
        
        return Order(
            order_id=str(result.get("id", "")),
            symbol=gate_symbol,
            side=Side.SHORT if position.side == Side.LONG else Side.LONG,
            order_type=OrderType.MARKET,
            size=size,
            price=None,
            filled_size=abs(float(result.get("size", 0)) - float(result.get("left", 0))),
            avg_price=float(result.get("fill_price", 0)) if result.get("fill_price") else 0,
            status=result.get("status", "open"),
            timestamp=datetime.now(timezone.utc),
            raw_data=result
        )
    
    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for symbol"""
        gate_symbol = symbol if "_" in symbol else get_exchange_symbol(symbol, Exchange.GATE)
        
        try:
            # Gate.io uses POST with query params for leverage
            params = {
                "contract": gate_symbol,
                "leverage": str(leverage)
            }
            await self._request("POST", "/api/v4/futures/usdt/positions/leverage", params=params)
            return True
        except Exception as e:
            print(f"Error setting Gate.io leverage: {e}")
            return False
    
    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """Get current funding rate"""
        gate_symbol = symbol if "_" in symbol else get_exchange_symbol(symbol, Exchange.GATE)
        
        result = await self._request("GET", f"/api/v4/futures/usdt/contracts/{gate_symbol}", signed=False)
        
        if result:
            # Gate.io funding_rate is already in decimal form (e.g., 0.0001 = 0.01%)
            funding_rate = float(result.get("funding_rate", 0))
            
            # Next funding time
            next_funding_time_s = int(result.get("funding_next_apply", 0))
            
            # Funding interval (typically 8 hours = 28800 seconds)
            funding_interval = int(result.get("funding_interval", 28800))
            funding_interval_hours = funding_interval // 3600
            
            return FundingRate(
                symbol=gate_symbol,
                funding_rate=funding_rate,
                next_funding_time=datetime.fromtimestamp(next_funding_time_s, tz=timezone.utc) if next_funding_time_s else datetime.now(timezone.utc),
                estimated_rate=float(result.get("funding_rate_indicative", 0)) if result.get("funding_rate_indicative") else None,
                funding_interval_hours=funding_interval_hours,
                raw_data=result
            )
        
        raise Exception(f"No funding rate data for {gate_symbol}")
    
    async def get_mark_price(self, symbol: str) -> float:
        """Get current mark price"""
        gate_symbol = symbol if "_" in symbol else get_exchange_symbol(symbol, Exchange.GATE)
        
        result = await self._request("GET", f"/api/v4/futures/usdt/contracts/{gate_symbol}", signed=False)
        
        if result:
            return float(result.get("mark_price", 0))
        
        raise Exception(f"No mark price data for {gate_symbol}")
    
    async def get_order_book(self, symbol: str, limit: int = 20) -> Dict[str, Any]:
        """Get order book (depth)"""
        gate_symbol = symbol if "_" in symbol else get_exchange_symbol(symbol, Exchange.GATE)
        
        params = {
            "contract": gate_symbol,
            "limit": str(limit)
        }
        
        result = await self._request("GET", "/api/v4/futures/usdt/order_book", params=params, signed=False)
        
        if result:
            bids = []
            asks = []
            
            # Gate.io format: {"asks": [{"p": "price", "s": size}], "bids": [...]}
            for b in result.get("bids", []):
                if isinstance(b, dict):
                    bids.append([float(b.get("p", 0)), float(b.get("s", 0))])
            
            for a in result.get("asks", []):
                if isinstance(a, dict):
                    asks.append([float(a.get("p", 0)), float(a.get("s", 0))])
            
            return {"bids": bids, "asks": asks}
        
        return {"bids": [], "asks": []}
    
    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """Get ticker data"""
        gate_symbol = symbol if "_" in symbol else get_exchange_symbol(symbol, Exchange.GATE)
        
        params = {"contract": gate_symbol}
        result = await self._request("GET", "/api/v4/futures/usdt/tickers", params=params, signed=False)
        
        if result and isinstance(result, list) and len(result) > 0:
            data = result[0]
            return {
                "symbol": gate_symbol,
                "last": float(data.get("last", 0)),
                "bid": float(data.get("highest_bid", 0)),
                "ask": float(data.get("lowest_ask", 0)),
                "volume": float(data.get("volume_24h", 0)),
                "raw": data
            }
        
        raise Exception(f"No ticker data for {gate_symbol}")
    
    async def set_position_mode(self, hedge_mode: bool = True) -> bool:
        """Set position mode (hedge or one-way)
        
        Note: Gate.io Futures uses dual_mode for hedge mode.
        dual_mode=true means hedge mode (can have both long and short)
        """
        try:
            # Gate.io requires specifying settle currency
            body = {"dual_mode": hedge_mode}
            await self._request("POST", "/api/v4/futures/usdt/dual_mode", body=body)
            return True
        except Exception as e:
            # Might fail if already in the requested mode
            if "already" in str(e).lower() or "same" in str(e).lower():
                return True
            print(f"Error setting Gate.io position mode: {e}")
            return False
    
    async def get_income_history(self, symbol: Optional[str] = None, limit: int = 100, income_type: str = "FUNDING_FEE") -> List[Dict]:
        """Get income history (funding fees, realized PnL, trading fees)
        
        Args:
            symbol: Gate.io symbol (e.g., BTC_USDT), optional
            limit: Maximum number of records to return
            income_type: Type of income - "FUNDING_FEE", "REALIZED_PNL", or "COMMISSION"
            
        Returns:
            List of income records with keys: symbol, income, time, type
        """
        income_list = []
        
        if income_type == "FUNDING_FEE":
            # Gate.io funding history endpoint
            params = {"limit": str(limit)}
            if symbol:
                gate_symbol = symbol if "_" in symbol else get_exchange_symbol(symbol, Exchange.GATE)
                params["contract"] = gate_symbol
            
            try:
                result = await self._request("GET", "/api/v4/futures/usdt/funding_payments", params=params)
                
                if result and isinstance(result, list):
                    for item in result:
                        income_list.append({
                            "symbol": item.get("contract", ""),
                            "income": float(item.get("payment", 0)),
                            "time": int(item.get("time", 0)) * 1000,  # Convert to ms
                            "type": "FUNDING_FEE"
                        })
            except Exception as e:
                print(f"Error getting Gate.io funding history: {e}")
        
        elif income_type == "REALIZED_PNL":
            # Get from position history (closed positions)
            params = {"limit": str(limit)}
            if symbol:
                gate_symbol = symbol if "_" in symbol else get_exchange_symbol(symbol, Exchange.GATE)
                params["contract"] = gate_symbol
            
            try:
                result = await self._request("GET", "/api/v4/futures/usdt/position_close", params=params)
                
                if result and isinstance(result, list):
                    for item in result:
                        income_list.append({
                            "symbol": item.get("contract", ""),
                            "income": float(item.get("pnl", 0)),
                            "time": int(item.get("time", 0)) * 1000,
                            "type": "REALIZED_PNL"
                        })
            except Exception as e:
                print(f"Error getting Gate.io PnL history: {e}")
        
        elif income_type == "COMMISSION":
            # Trading fees from order history
            params = {"limit": str(limit), "status": "finished"}
            if symbol:
                gate_symbol = symbol if "_" in symbol else get_exchange_symbol(symbol, Exchange.GATE)
                params["contract"] = gate_symbol
            
            try:
                result = await self._request("GET", "/api/v4/futures/usdt/orders", params=params)
                
                if result and isinstance(result, list):
                    for item in result:
                        fee = float(item.get("fee", 0))
                        if fee != 0:
                            income_list.append({
                                "symbol": item.get("contract", ""),
                                "income": -abs(fee),  # Fees are negative income
                                "time": int(item.get("finish_time", item.get("create_time", 0))) * 1000,
                                "type": "COMMISSION"
                            })
            except Exception as e:
                print(f"Error getting Gate.io commission history: {e}")
        
        return income_list
    
    async def get_recent_orders(self, symbol: str, limit: int = 10) -> List[Dict]:
        """Get recent orders for a symbol
        
        Args:
            symbol: Trading pair symbol
            limit: Maximum number of orders to return
            
        Returns:
            List of recent orders
        """
        gate_symbol = symbol if "_" in symbol else get_exchange_symbol(symbol, Exchange.GATE)
        
        params = {
            "contract": gate_symbol,
            "limit": str(limit),
            "status": "finished"
        }
        
        try:
            result = await self._request("GET", "/api/v4/futures/usdt/orders", params=params)
            if result and isinstance(result, list):
                return result
            return []
        except Exception as e:
            print(f"Error getting Gate.io order history: {e}")
            return []
            
    async def get_closed_pnl(self, symbol: str, since: Optional[int] = None) -> Dict[str, Any]:
        """Get realized PnL, commission fees, and funding fees for a closed position"""
        gate_symbol = symbol if "_" in symbol else get_exchange_symbol(symbol, Exchange.GATE)
        
        realized_pnl = 0.0
        commission = 0.0
        funding_fee = 0.0
        
        try:
            # We use the existing get_income_history method
            res_pnl = await self.get_income_history(gate_symbol, limit=100, income_type="REALIZED_PNL")
            if since:
                res_pnl = [item for item in res_pnl if item.get("time", 0) >= since]
            for item in res_pnl:
                realized_pnl += float(item.get("income", 0))
                
            res_fee = await self.get_income_history(gate_symbol, limit=100, income_type="COMMISSION")
            if since:
                res_fee = [item for item in res_fee if item.get("time", 0) >= since]
            for item in res_fee:
                commission += abs(float(item.get("income", 0)))
                
            res_fund = await self.get_income_history(gate_symbol, limit=100, income_type="FUNDING_FEE")
            if since:
                res_fund = [item for item in res_fund if item.get("time", 0) >= since]
            for item in res_fund:
                funding_fee += float(item.get("income", 0))
                
            net_pnl = realized_pnl - commission + funding_fee
            
            return {
                "realized_pnl": realized_pnl,
                "commission": commission,
                "funding_fee": funding_fee,
                "net_pnl": net_pnl
            }
        except Exception as e:
            print(f"Error getting Gate PnL: {e}")
            return {
                "realized_pnl": 0.0,
                "commission": 0.0,
                "funding_fee": 0.0,
                "net_pnl": 0.0
            }
    
    def get_exchange_name(self) -> str:
        """Get exchange name"""
        return "Gate.io"

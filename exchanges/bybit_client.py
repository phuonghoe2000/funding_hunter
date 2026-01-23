"""
Bybit Futures API Client (V5 API)
"""
import hmac
import hashlib
import time
import json
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from urllib.parse import urlencode
import aiohttp

from config.settings import BybitConfig
from config.constants import Side, OrderType, PositionStatus, Exchange, get_exchange_symbol
from .base import BaseExchangeClient, Position, Order, FundingRate, Balance


class BybitClient(BaseExchangeClient):
    """Bybit Futures API Client (V5 API)"""
    
    def __init__(self, config: BybitConfig, debug: bool = False):
        super().__init__(config.api_key, config.secret_key)
        self.config = config
        self.base_url = config.base_url
        self._session: Optional[aiohttp.ClientSession] = None
        self.debug = debug
        self.recv_window = 5000
    
    def _get_timestamp(self) -> int:
        """Get current timestamp in milliseconds"""
        return int(time.time() * 1000)
    
    def _sign(self, timestamp: int, params: Dict[str, Any]) -> str:
        """Generate signature for API request (V5 API)"""
        # Bybit V5 signature: timestamp + api_key + recv_window + queryString
        param_str = urlencode(sorted(params.items())) if params else ""
        sign_str = f"{timestamp}{self.api_key}{self.recv_window}{param_str}"
        
        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            sign_str.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature
    
    def _sign_post(self, timestamp: int, params: Dict[str, Any]) -> str:
        """Generate signature for POST request (V5 API)"""
        # For POST, the body is JSON stringified
        param_str = json.dumps(params) if params else ""
        sign_str = f"{timestamp}{self.api_key}{self.recv_window}{param_str}"
        
        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            sign_str.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature
    
    def _get_headers(self, timestamp: int, signature: str) -> Dict[str, str]:
        """Get headers for API request"""
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": str(timestamp),
            "X-BAPI-RECV-WINDOW": str(self.recv_window),
            "Content-Type": "application/json"
        }
    
    async def connect(self) -> bool:
        """Connect to Bybit API"""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        
        try:
            balance = await self.get_balance()
            return balance is not None
        except Exception as e:
            print(f"Bybit connection error: {e}")
            if self._session:
                await self._session.close()
                self._session = None
            return False
    
    async def disconnect(self):
        """Disconnect from Bybit API"""
        if self._session:
            await self._session.close()
            await asyncio.sleep(0.25)
            self._session = None
    
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
        timestamp = self._get_timestamp()
        
        if signed:
            if method == "GET":
                signature = self._sign(timestamp, params)
            else:
                signature = self._sign_post(timestamp, params)
            headers = self._get_headers(timestamp, signature)
        else:
            headers = {"Content-Type": "application/json"}
        
        if self.debug:
            print(f"\n{'='*60}")
            print(f"[BYBIT REQUEST]")
            print(f"  Method: {method}")
            print(f"  URL: {url}")
            print(f"  Params: {json.dumps(params, indent=2)}")
            print(f"  Headers: X-BAPI-API-KEY: {self.api_key[:8]}...")
        
        req_timeout = aiohttp.ClientTimeout(total=timeout)
        
        try:
            if method == "GET":
                async with self._session.get(url, headers=headers, params=params, timeout=req_timeout) as response:
                    status_code = response.status
                    result = await response.json()
            else:
                async with self._session.post(url, headers=headers, json=params, timeout=req_timeout) as response:
                    status_code = response.status
                    result = await response.json()
        except asyncio.TimeoutError:
            raise Exception(f"Bybit API request timeout after {timeout}s")
        
        if self.debug:
            print(f"[BYBIT RESPONSE]")
            print(f"  Status: {status_code}")
            print(f"  Data: {json.dumps(result, indent=2)[:2000]}")
            print(f"{'='*60}\n")
        
        # Check for error
        if result.get("retCode") != 0:
            raise Exception(f"Bybit API Error: {result.get('retMsg', 'Unknown error')} (code: {result.get('retCode')})")
        
        return result.get("result", {})
    
    async def get_balance(self, currency: str = "USDT") -> Balance:
        """Get account balance"""
        result = await self._request("GET", "/v5/account/wallet-balance", {
            "accountType": "UNIFIED"
        })
        
        for account in result.get("list", []):
            for coin in account.get("coin", []):
                if coin.get("coin") == currency:
                    return Balance(
                        currency=currency,
                        total=float(coin.get("walletBalance", 0)),
                        available=float(coin.get("availableToWithdraw", 0)),
                        frozen=float(coin.get("walletBalance", 0)) - float(coin.get("availableToWithdraw", 0)),
                        raw_data=coin
                    )
        
        return Balance(currency=currency, total=0, available=0, frozen=0)
    
    async def get_position(self, symbol: str) -> Optional[Position]:
        """Get position for symbol"""
        bybit_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BYBIT)
        
        result = await self._request("GET", "/v5/position/list", {
            "category": "linear",
            "symbol": bybit_symbol
        })
        
        for pos_data in result.get("list", []):
            if pos_data.get("symbol") == bybit_symbol:
                pos_size = float(pos_data.get("size", 0))
                
                if pos_size == 0:
                    continue
                
                side_str = pos_data.get("side", "")
                side = Side.LONG if side_str == "Buy" else Side.SHORT
                
                return Position(
                    symbol=bybit_symbol,
                    side=side,
                    size=abs(pos_size),
                    entry_price=float(pos_data.get("avgPrice", 0)),
                    mark_price=float(pos_data.get("markPrice", 0)),
                    liquidation_price=float(pos_data.get("liqPrice", 0) or 0),
                    unrealized_pnl=float(pos_data.get("unrealisedPnl", 0)),
                    leverage=int(float(pos_data.get("leverage", 1))),
                    status=PositionStatus.OPEN,
                    timestamp=datetime.now(timezone.utc),
                    raw_data=pos_data
                )
        
        return None
    
    async def get_all_positions(self) -> List[Position]:
        """Get all open positions"""
        result = await self._request("GET", "/v5/position/list", {
            "category": "linear",
            "settleCoin": "USDT"
        })
        
        positions = []
        for pos_data in result.get("list", []):
            pos_size = float(pos_data.get("size", 0))
            
            if pos_size == 0:
                continue
            
            side_str = pos_data.get("side", "")
            side = Side.LONG if side_str == "Buy" else Side.SHORT
            
            positions.append(Position(
                symbol=pos_data.get("symbol"),
                side=side,
                size=abs(pos_size),
                entry_price=float(pos_data.get("avgPrice", 0)),
                mark_price=float(pos_data.get("markPrice", 0)),
                liquidation_price=float(pos_data.get("liqPrice", 0) or 0),
                unrealized_pnl=float(pos_data.get("unrealisedPnl", 0)),
                leverage=int(float(pos_data.get("leverage", 1))),
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
        bybit_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BYBIT)
        
        params = {
            "category": "linear",
            "symbol": bybit_symbol,
            "side": "Buy" if side == Side.LONG else "Sell",
            "orderType": "Market",
            "qty": str(size),
        }
        
        if reduce_only:
            params["reduceOnly"] = True
        else:
            # Set position index for hedge mode (1=Buy side, 2=Sell side)
            params["positionIdx"] = 1 if side == Side.LONG else 2
        
        result = await self._request("POST", "/v5/order/create", params)
        
        return Order(
            order_id=str(result.get("orderId", "")),
            symbol=bybit_symbol,
            side=side,
            order_type=OrderType.MARKET,
            size=size,
            price=None,
            filled_size=size,  # Market orders fill immediately
            avg_price=0,  # Will be updated by exchange
            status="Filled",
            timestamp=datetime.now(timezone.utc),
            raw_data=result
        )
    
    async def close_position(self, symbol: str, aggressive: bool = False) -> Order:
        """Close position for symbol"""
        bybit_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BYBIT)
        
        position = await self.get_position(bybit_symbol)
        
        if not position:
            raise Exception(f"No position found for {bybit_symbol}")
        
        close_side = "Sell" if position.side == Side.LONG else "Buy"
        
        params = {
            "category": "linear",
            "symbol": bybit_symbol,
            "side": close_side,
            "orderType": "Market",
            "qty": str(position.size),
            "positionIdx": 1 if position.side == Side.LONG else 2,
            "reduceOnly": True
        }
        
        result = await self._request("POST", "/v5/order/create", params)
        
        return Order(
            order_id=str(result.get("orderId", "")),
            symbol=bybit_symbol,
            side=Side.SHORT if position.side == Side.LONG else Side.LONG,
            order_type=OrderType.MARKET,
            size=position.size,
            price=None,
            filled_size=position.size,
            avg_price=0,
            status="Filled",
            timestamp=datetime.now(timezone.utc),
            raw_data=result
        )
    
    async def close_position_partial(self, symbol: str, size: float) -> Order:
        """Close partial position with specific size"""
        bybit_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BYBIT)
        
        position = await self.get_position(bybit_symbol)
        
        if not position:
            raise Exception(f"No position found for {bybit_symbol}")
        
        size = self._round_quantity(bybit_symbol, size)
        close_side = "Sell" if position.side == Side.LONG else "Buy"
        
        params = {
            "category": "linear",
            "symbol": bybit_symbol,
            "side": close_side,
            "orderType": "Market",
            "qty": str(size),
            "positionIdx": 1 if position.side == Side.LONG else 2,
            "reduceOnly": True
        }
        
        result = await self._request("POST", "/v5/order/create", params)
        
        return Order(
            order_id=str(result.get("orderId", "")),
            symbol=bybit_symbol,
            side=Side.SHORT if position.side == Side.LONG else Side.LONG,
            order_type=OrderType.MARKET,
            size=size,
            price=None,
            filled_size=size,
            avg_price=0,
            status="Filled",
            timestamp=datetime.now(timezone.utc),
            raw_data=result
        )
    
    def _round_quantity(self, symbol: str, quantity: float) -> float:
        """Round quantity to appropriate precision for symbol"""
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
        bybit_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BYBIT)
        
        try:
            await self._request("POST", "/v5/position/set-leverage", {
                "category": "linear",
                "symbol": bybit_symbol,
                "buyLeverage": str(leverage),
                "sellLeverage": str(leverage)
            })
            return True
        except Exception as e:
            # Might fail if leverage is already set
            if "leverage not modified" in str(e).lower():
                return True
            print(f"Error setting leverage: {e}")
            return False
    
    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """Get current funding rate"""
        bybit_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BYBIT)
        
        result = await self._request("GET", "/v5/market/tickers", {
            "category": "linear",
            "symbol": bybit_symbol
        }, signed=False)
        
        ticker = result.get("list", [{}])[0]
        
        # Get funding rate interval info
        info_result = await self._request("GET", "/v5/market/instruments-info", {
            "category": "linear",
            "symbol": bybit_symbol
        }, signed=False)
        
        instrument = info_result.get("list", [{}])[0]
        funding_interval_mins = int(instrument.get("fundingInterval", 480))
        funding_interval_hours = funding_interval_mins / 60
        
        next_funding_ts = int(ticker.get("nextFundingTime", 0))
        next_funding_time = datetime.fromtimestamp(next_funding_ts / 1000, tz=timezone.utc) if next_funding_ts else datetime.now(timezone.utc)
        
        return FundingRate(
            symbol=bybit_symbol,
            funding_rate=float(ticker.get("fundingRate", 0)),
            next_funding_time=next_funding_time,
            estimated_rate=float(ticker.get("fundingRate", 0)),
            funding_interval_hours=funding_interval_hours,
            raw_data=ticker
        )
    
    async def get_all_funding_rates(self) -> List[FundingRate]:
        """Get all funding rates for all perpetual contracts"""
        result = await self._request("GET", "/v5/market/tickers", {
            "category": "linear"
        }, signed=False)
        
        funding_rates = []
        for item in result.get("list", []):
            symbol = item.get("symbol", "")
            if symbol.endswith("USDT"):
                next_funding_ts = int(item.get("nextFundingTime", 0))
                next_funding_time = datetime.fromtimestamp(next_funding_ts / 1000, tz=timezone.utc) if next_funding_ts else datetime.now(timezone.utc)
                
                funding_rates.append(FundingRate(
                    symbol=symbol,
                    funding_rate=float(item.get("fundingRate", 0)),
                    next_funding_time=next_funding_time,
                    estimated_rate=float(item.get("fundingRate", 0)),
                    funding_interval_hours=8,  # Default to 8h
                    raw_data=item
                ))
        
        return funding_rates
    
    async def get_mark_price(self, symbol: str) -> float:
        """Get current mark price"""
        bybit_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BYBIT)
        
        result = await self._request("GET", "/v5/market/tickers", {
            "category": "linear",
            "symbol": bybit_symbol
        }, signed=False)
        
        ticker = result.get("list", [{}])[0]
        return float(ticker.get("markPrice", 0))
    
    async def get_order_book(self, symbol: str, limit: int = 20) -> Dict[str, Any]:
        """Get order book (depth)"""
        bybit_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BYBIT)
        
        result = await self._request("GET", "/v5/market/orderbook", {
            "category": "linear",
            "symbol": bybit_symbol,
            "limit": limit
        }, signed=False)
        
        return {
            "bids": [[float(price), float(qty)] for price, qty in result.get("b", [])],
            "asks": [[float(price), float(qty)] for price, qty in result.get("a", [])]
        }
    
    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """Get ticker data"""
        bybit_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BYBIT)
        
        result = await self._request("GET", "/v5/market/tickers", {
            "category": "linear",
            "symbol": bybit_symbol
        }, signed=False)
        
        ticker = result.get("list", [{}])[0]
        
        return {
            "symbol": bybit_symbol,
            "last": float(ticker.get("lastPrice", 0)),
            "bid": float(ticker.get("bid1Price", 0)),
            "ask": float(ticker.get("ask1Price", 0)),
            "volume": float(ticker.get("volume24h", 0)),
            "raw": ticker
        }
    
    async def set_position_mode(self, hedge_mode: bool = True) -> bool:
        """Set position mode (hedge or one-way)"""
        try:
            await self._request("POST", "/v5/position/switch-mode", {
                "category": "linear",
                "mode": 3 if hedge_mode else 0  # 3=Hedge mode, 0=One-way mode
            })
            return True
        except Exception as e:
            # Might fail if already in the requested mode
            if "position mode is not modified" in str(e).lower():
                return True
            print(f"Error setting position mode: {e}")
            return False
    
    async def get_income_history(self, income_type: str = "FUNDING_FEE", limit: int = 100, symbol: Optional[str] = None) -> List[Dict]:
        """Get income history (funding fees, etc.)"""
        params = {
            "category": "linear",
            "limit": limit
        }
        
        if symbol:
            bybit_symbol = symbol if "USDT" in symbol and "/" not in symbol else symbol.replace('/', '')
            params["symbol"] = bybit_symbol
        
        # Map income type
        if income_type == "FUNDING_FEE":
            params["type"] = "Funding"
        elif income_type == "REALIZED_PNL":
            params["type"] = "RealisedPnL"
        
        result = await self._request("GET", "/v5/account/transaction-log", params)
        
        return result.get("list", [])
    
    async def get_recent_orders(self, symbol: str, limit: int = 10) -> List[Dict]:
        """Get recent orders for a symbol"""
        bybit_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BYBIT)
        
        result = await self._request("GET", "/v5/order/history", {
            "category": "linear",
            "symbol": bybit_symbol,
            "limit": limit
        })
        
        return result.get("list", [])
    
    def get_exchange_name(self) -> str:
        """Get exchange name"""
        return "Bybit"

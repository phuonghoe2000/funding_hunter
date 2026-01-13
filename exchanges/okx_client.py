"""
OKX Futures API Client
"""
import hmac
import base64
import hashlib
import json
import time
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
import aiohttp

from config.settings import OKXConfig
from config.constants import Side, OrderType, PositionStatus, Exchange, get_exchange_symbol
from .base import BaseExchangeClient, Position, Order, FundingRate, Balance


class OKXClient(BaseExchangeClient):
    """OKX Futures API Client"""
    
    def __init__(self, config: OKXConfig, debug: bool = False):
        super().__init__(config.api_key, config.secret_key)
        self.config = config
        self.passphrase = config.passphrase
        self.base_url = config.base_url
        self._session: Optional[aiohttp.ClientSession] = None
        self.debug = debug
    
    def _get_timestamp(self) -> str:
        """Get ISO format timestamp"""
        return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    
    def _sign(self, timestamp: str, method: str, request_path: str, body: str = "") -> str:
        """Generate signature for API request"""
        message = timestamp + method + request_path + body
        mac = hmac.new(
            self.secret_key.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        )
        return base64.b64encode(mac.digest()).decode('utf-8')
    
    def _get_headers(self, method: str, request_path: str, body: str = "") -> Dict[str, str]:
        """Generate headers for API request"""
        timestamp = self._get_timestamp()
        sign = self._sign(timestamp, method, request_path, body)
        
        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json"
        }
        
        if self.config.testnet:
            headers["x-simulated-trading"] = "1"
        
        return headers
    
    async def connect(self) -> bool:
        """Connect to OKX API"""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        
        # Test connection by getting account info
        try:
            balance = await self.get_balance()
            return balance is not None
        except Exception as e:
            print(f"OKX connection error: {e}")
            # Close session on failed connection to prevent leak
            if self._session:
                await self._session.close()
                self._session = None
            return False
    
    async def disconnect(self):
        """Disconnect from OKX API"""
        if self._session:
            await self._session.close()
            # Give time for the underlying connections to close
            import asyncio
            await asyncio.sleep(0.25)
            self._session = None
    
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        data: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Make API request"""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        
        url = f"{self.base_url}{endpoint}"
        body = ""
        
        if method == "GET" and params:
            query = "&".join([f"{k}={v}" for k, v in params.items()])
            endpoint = f"{endpoint}?{query}"
            url = f"{self.base_url}{endpoint}"
        elif data:
            body = json.dumps(data)
        
        headers = self._get_headers(method, endpoint, body)
        
        # Debug: Print request details
        if self.debug:
            print(f"\n{'='*60}")
            print(f"[OKX REQUEST]")
            print(f"  Method: {method}")
            print(f"  URL: {url}")
            print(f"  Body: {body if body else 'None'}")
            print(f"  Headers: OK-ACCESS-KEY: {self.api_key[:8]}...")
        
        # Add timeout to prevent hanging
        req_timeout = aiohttp.ClientTimeout(total=15.0)
        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                data=body if body else None,
                timeout=req_timeout
            ) as response:
                result = await response.json()
                
                # Debug: Print response
                if self.debug:
                    print(f"[OKX RESPONSE]")
                    print(f"  Status: {response.status}")
                    print(f"  Data: {json.dumps(result, indent=2)[:1000]}")
                    print(f"{'='*60}\n")
                
                if result.get("code") != "0":
                    raise Exception(f"OKX API Error: {result.get('msg', 'Unknown error')}")
                
                return result
        except asyncio.TimeoutError:
            raise Exception(f"OKX API request timeout after 15s")
    
    async def get_balance(self, currency: str = "USDT") -> Balance:
        """Get account balance"""
        result = await self._request("GET", "/api/v5/account/balance", {"ccy": currency})
        
        if result["data"]:
            for detail in result["data"][0].get("details", []):
                if detail["ccy"] == currency:
                    return Balance(
                        currency=currency,
                        total=float(detail.get("eq", 0)),
                        available=float(detail.get("availBal", 0)),
                        frozen=float(detail.get("frozenBal", 0)),
                        raw_data=detail
                    )
        
        return Balance(currency=currency, total=0, available=0, frozen=0)
    
    async def get_position(self, symbol: str) -> Optional[Position]:
        """Get position for symbol"""
        # Convert to OKX symbol format if needed
        okx_symbol = symbol if "-SWAP" in symbol else get_exchange_symbol(symbol, Exchange.OKX)
        
        result = await self._request("GET", "/api/v5/account/positions", {"instId": okx_symbol})
        
        if result["data"]:
            pos_data = result["data"][0]
            pos_side = pos_data.get("posSide", "")
            pos_amt = float(pos_data.get("pos", 0))
            
            if pos_amt == 0:
                return None
            
            # Determine side
            if pos_side == "long" or pos_amt > 0:
                side = Side.LONG
            else:
                side = Side.SHORT
            
            return Position(
                symbol=okx_symbol,
                side=side,
                size=abs(pos_amt),
                entry_price=float(pos_data.get("avgPx", 0)),
                mark_price=float(pos_data.get("markPx", 0)),
                liquidation_price=float(pos_data.get("liqPx", 0)) if pos_data.get("liqPx") else 0,
                unrealized_pnl=float(pos_data.get("upl", 0)),
                leverage=int(pos_data.get("lever", 1)),
                status=PositionStatus.OPEN,
                timestamp=datetime.now(timezone.utc),
                raw_data=pos_data
            )
        
        return None
    
    async def get_all_positions(self) -> List[Position]:
        """Get all open positions"""
        result = await self._request("GET", "/api/v5/account/positions", {"instType": "SWAP"})
        
        positions = []
        for pos_data in result.get("data", []):
            pos_amt = float(pos_data.get("pos", 0))
            if pos_amt == 0:
                continue
            
            pos_side = pos_data.get("posSide", "")
            if pos_side == "long" or pos_amt > 0:
                side = Side.LONG
            else:
                side = Side.SHORT
            
            positions.append(Position(
                symbol=pos_data["instId"],
                side=side,
                size=abs(pos_amt),
                entry_price=float(pos_data.get("avgPx", 0)),
                mark_price=float(pos_data.get("markPx", 0)),
                liquidation_price=float(pos_data.get("liqPx", 0)) if pos_data.get("liqPx") else 0,
                unrealized_pnl=float(pos_data.get("upl", 0)),
                leverage=int(pos_data.get("lever", 1)),
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
        okx_symbol = symbol if "-SWAP" in symbol else get_exchange_symbol(symbol, Exchange.OKX)
        
        # OKX uses tdMode for trade mode (cross/isolated)
        # posSide for position direction
        order_data = {
            "instId": okx_symbol,
            "tdMode": "cross",  # Cross margin
            "side": "buy" if side == Side.LONG else "sell",
            "ordType": "market",
            "sz": str(size),
        }
        
        # For net mode
        if reduce_only:
            order_data["reduceOnly"] = "true"
        else:
            # Set position side for hedge mode
            order_data["posSide"] = "long" if side == Side.LONG else "short"
        
        result = await self._request("POST", "/api/v5/trade/order", data=order_data)
        
        order_info = result["data"][0]
        
        return Order(
            order_id=order_info["ordId"],
            symbol=okx_symbol,
            side=side,
            order_type=OrderType.MARKET,
            size=size,
            price=None,
            filled_size=0,  # Will be updated
            avg_price=0,
            status="submitted",
            timestamp=datetime.now(timezone.utc),
            raw_data=order_info
        )
    
    async def close_position(self, symbol: str, aggressive: bool = False) -> Order:
        """Close position for symbol
        
        Args:
            symbol: Trading symbol
            aggressive: If True, uses aggressive limit order (5% off market price) for guaranteed fill
        """
        okx_symbol = symbol if "-SWAP" in symbol else get_exchange_symbol(symbol, Exchange.OKX)
        
        # Get current position
        position = await self.get_position(okx_symbol)
        
        if not position:
            raise Exception(f"No position found for {okx_symbol}")
        
        # Close by placing opposite order
        close_side = Side.SHORT if position.side == Side.LONG else Side.LONG
        
        if aggressive:
            # AGGRESSIVE MODE: Use limit order with extreme price
            current_price = position.mark_price
            
            # For SELL (closing LONG): set price 5% below market
            # For BUY (closing SHORT): set price 5% above market
            if position.side == Side.LONG:
                aggressive_price = current_price * 0.95  # SELL 5% below
            else:
                aggressive_price = current_price * 1.05  # BUY 5% above
            
            # Round to appropriate decimals
            if aggressive_price > 1000:
                aggressive_price = round(aggressive_price, 1)
            elif aggressive_price > 100:
                aggressive_price = round(aggressive_price, 2)
            else:
                aggressive_price = round(aggressive_price, 3)
            
            order_data = {
                "instId": okx_symbol,
                "tdMode": "cross",
                "side": "sell" if position.side == Side.LONG else "buy",
                "ordType": "ioc",  # Immediate or Cancel
                "px": str(aggressive_price),  # Aggressive price
                "sz": str(position.size),
                "posSide": "long" if position.side == Side.LONG else "short",
                "reduceOnly": "true"
            }
        else:
            # STANDARD MODE: Market close order
            order_data = {
                "instId": okx_symbol,
                "tdMode": "cross",
                "side": "sell" if position.side == Side.LONG else "buy",
                "ordType": "market",
                "sz": str(position.size),
                "posSide": "long" if position.side == Side.LONG else "short",
                "reduceOnly": "true"
            }
        
        result = await self._request("POST", "/api/v5/trade/order", data=order_data)
        
        order_info = result["data"][0]
        
        return Order(
            order_id=order_info["ordId"],
            symbol=okx_symbol,
            side=close_side,
            order_type=OrderType.MARKET,
            size=position.size,
            price=None,
            filled_size=0,
            avg_price=0,
            status="submitted",
            timestamp=datetime.now(timezone.utc),
            raw_data=order_info
        )
    
    async def close_position_partial(self, symbol: str, size: float) -> Order:
        """Close partial position with specific size"""
        okx_symbol = symbol if "-SWAP" in symbol else get_exchange_symbol(symbol, Exchange.OKX)
        
        position = await self.get_position(okx_symbol)
        
        if not position:
            raise Exception(f"No position found for {okx_symbol}")
        
        # Round size to appropriate precision
        size = self._round_quantity(size)
        
        close_side = Side.SHORT if position.side == Side.LONG else Side.LONG
        
        order_data = {
            "instId": okx_symbol,
            "tdMode": "cross",
            "side": "sell" if position.side == Side.LONG else "buy",
            "ordType": "market",
            "sz": str(size),
            "posSide": "long" if position.side == Side.LONG else "short",
            "reduceOnly": "true"
        }
        
        result = await self._request("POST", "/api/v5/trade/order", data=order_data)
        
        order_info = result["data"][0]
        
        return Order(
            order_id=order_info["ordId"],
            symbol=okx_symbol,
            side=close_side,
            order_type=OrderType.MARKET,
            size=size,
            price=None,
            filled_size=0,
            avg_price=0,
            status="submitted",
            timestamp=datetime.now(timezone.utc),
            raw_data=order_info
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
        okx_symbol = symbol if "-SWAP" in symbol else get_exchange_symbol(symbol, Exchange.OKX)
        
        # Set for both long and short positions
        for pos_side in ["long", "short"]:
            data = {
                "instId": okx_symbol,
                "lever": str(leverage),
                "mgnMode": "cross",
                "posSide": pos_side
            }
            
            try:
                await self._request("POST", "/api/v5/account/set-leverage", data=data)
            except Exception as e:
                print(f"Error setting leverage for {pos_side}: {e}")
                return False
        
        return True
    
    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """Get current funding rate"""
        okx_symbol = symbol if "-SWAP" in symbol else get_exchange_symbol(symbol, Exchange.OKX)
        
        result = await self._request("GET", "/api/v5/public/funding-rate", {"instId": okx_symbol})
        
        if result["data"]:
            data = result["data"][0]
            next_funding = int(data.get("nextFundingTime", 0))
            
            # OKX typically uses 8-hour intervals
            # fundingTime field contains the interval in milliseconds if present
            funding_interval_hours = 8
            if "fundingTime" in data:
                interval_ms = int(data["fundingTime"])
                funding_interval_hours = interval_ms // (1000 * 3600)  # Convert ms to hours
            
            return FundingRate(
                symbol=okx_symbol,
                funding_rate=float(data.get("fundingRate", 0)),
                next_funding_time=datetime.fromtimestamp(next_funding / 1000, tz=timezone.utc),
                estimated_rate=float(data.get("nextFundingRate", 0)) if data.get("nextFundingRate") else None,
                funding_interval_hours=funding_interval_hours,
                raw_data=data
            )
        
        raise Exception(f"No funding rate data for {okx_symbol}")
    
    async def get_mark_price(self, symbol: str) -> float:
        """Get current mark price"""
        okx_symbol = symbol if "-SWAP" in symbol else get_exchange_symbol(symbol, Exchange.OKX)
        
        result = await self._request("GET", "/api/v5/public/mark-price", {"instId": okx_symbol})
        
        if result["data"]:
            return float(result["data"][0]["markPx"])
        
        raise Exception(f"No mark price data for {okx_symbol}")
    
    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """Get ticker data"""
        okx_symbol = symbol if "-SWAP" in symbol else get_exchange_symbol(symbol, Exchange.OKX)
        
        result = await self._request("GET", "/api/v5/market/ticker", {"instId": okx_symbol})
        
        if result["data"]:
            data = result["data"][0]
            return {
                "symbol": okx_symbol,
                "last": float(data.get("last", 0)),
                "bid": float(data.get("bidPx", 0)),
                "ask": float(data.get("askPx", 0)),
                "volume": float(data.get("vol24h", 0)),
                "raw": data
            }
        
        raise Exception(f"No ticker data for {okx_symbol}")
    
    async def set_position_mode(self, hedge_mode: bool = True) -> bool:
        """Set position mode (hedge or one-way)"""
        data = {
            "posMode": "long_short_mode" if hedge_mode else "net_mode"
        }
        
        try:
            await self._request("POST", "/api/v5/account/set-position-mode", data=data)
            return True
        except Exception as e:
            print(f"Error setting position mode: {e}")
            return False
    
    async def get_income_history(self, symbol: Optional[str] = None, limit: int = 100) -> List[Dict]:
        """Get income history (funding fees, etc.)
        
        Args:
            symbol: OKX symbol (e.g., BTC-USDT-SWAP), optional
            limit: Maximum number of records to return
            
        Returns:
            List of income records with keys: symbol, income, time, type
        """
        params = {
            "instType": "SWAP",
            "type": "8",  # 8 = funding fee
            "limit": str(limit)
        }
        
        if symbol:
            okx_symbol = symbol if "-SWAP" in symbol else get_exchange_symbol(symbol, Exchange.OKX)
            params["instId"] = okx_symbol
        
        try:
            result = await self._request("GET", "/api/v5/account/bills", params)
            
            # Transform to common format
            income_list = []
            for item in result.get("data", []):
                income_list.append({
                    "symbol": item.get("instId", ""),
                    "income": float(item.get("balChg", 0)),  # Balance change (can be positive or negative)
                    "time": int(item.get("ts", 0)),  # Timestamp in milliseconds
                    "type": "FUNDING_FEE"
                })
            
            return income_list
        except Exception as e:
            print(f"Error getting OKX income history: {e}")
            return []
    
    def get_exchange_name(self) -> str:
        """Get exchange name"""
        return "OKX"

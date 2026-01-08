"""
BingX Futures API Client
"""
import hmac
import hashlib
import time
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from urllib.parse import urlencode
import aiohttp

from config.settings import BingXConfig
from config.constants import Side, OrderType, PositionStatus, Exchange, get_exchange_symbol
from .base import BaseExchangeClient, Position, Order, FundingRate, Balance


class BingXClient(BaseExchangeClient):
    """BingX Futures API Client"""
    
    def __init__(self, config: 'BingXConfig', debug: bool = False):
        super().__init__(config.api_key, config.secret_key)
        self.config = config
        self.base_url = config.base_url
        self._session: Optional[aiohttp.ClientSession] = None
        self.debug = debug
    
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
            return balance is not None
        except Exception as e:
            print(f"BingX connection error: {e}")
            return False
    
    async def disconnect(self):
        """Disconnect from BingX API"""
        if self._session:
            await self._session.close()
            self._session = None
    
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
        try:
            async with self._session.request(method, full_url, headers=headers) as response:
                result = await response.json()
                
                # Debug: Print response
                if self.debug:
                    print(f"[BINGX RESPONSE]")
                    print(f"  Status: {response.status}")
                    print(f"  Data: {json.dumps(result, indent=2)[:1000]}")
                    print(f"{'='*60}\n")
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
    
    async def get_position(self, symbol: str) -> Optional[Position]:
        """Get position for symbol"""
        # Convert to BingX symbol format if needed
        bingx_symbol = symbol if "-" in symbol else get_exchange_symbol(symbol, Exchange.BINGX)
        
        result = await self._request("GET", "/openApi/swap/v2/user/positions", {"symbol": bingx_symbol})
        
        if result.get("code") == 0 and result.get("data"):
            for pos_data in result["data"]:
                if pos_data.get("symbol") == bingx_symbol:
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
        """Get current funding rate"""
        bingx_symbol = symbol if "-" in symbol else get_exchange_symbol(symbol, Exchange.BINGX)
        
        result = await self._request("GET", "/openApi/swap/v2/quote/premiumIndex", 
                                     {"symbol": bingx_symbol}, signed=False)
        
        if result.get("code") == 0 and result.get("data"):
            data = result["data"]
            next_funding_time_ms = int(data.get("nextFundingTime", 0))
            
            # BingX typically uses 8-hour intervals
            funding_interval_hours = 8
            
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
            await self._request("POST", "/openApi/swap/v2/trade/positionSide/dual", {
                "dualSidePosition": "true" if hedge_mode else "false"
            })
            return True
        except Exception as e:
            # Might fail if already in the requested mode
            if "No need to change" in str(e) or "already" in str(e).lower():
                return True
            print(f"Error setting position mode: {e}")
            return False
    
    async def get_income_history(self, symbol: Optional[str] = None, limit: int = 100) -> List[Dict]:
        """Get income history (funding fees, etc.)
        
        Args:
            symbol: BingX symbol (e.g., BTC-USDT), optional
            limit: Maximum number of records to return
            
        Returns:
            List of income records with keys: symbol, income, time, type
        """
        params = {
            "incomeType": "FUNDING_FEE",
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
                            "type": "FUNDING_FEE"
                        })
            
            return income_list
        except Exception as e:
            print(f"Error getting BingX income history: {e}")
            return []
    
    def get_exchange_name(self) -> str:
        """Get exchange name"""
        return "BingX"

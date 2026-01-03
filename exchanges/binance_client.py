"""
Binance Futures API Client
"""
import hmac
import hashlib
import time
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from urllib.parse import urlencode
import aiohttp

from config.settings import BinanceConfig
from config.constants import Side, OrderType, PositionStatus, Exchange, get_exchange_symbol
from .base import BaseExchangeClient, Position, Order, FundingRate, Balance


class BinanceClient(BaseExchangeClient):
    """Binance Futures API Client"""
    
    def __init__(self, config: BinanceConfig, debug: bool = False):
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
    
    async def connect(self) -> bool:
        """Connect to Binance API"""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        
        try:
            balance = await self.get_balance()
            return balance is not None
        except Exception as e:
            print(f"Binance connection error: {e}")
            return False
    
    async def disconnect(self):
        """Disconnect from Binance API"""
        if self._session:
            await self._session.close()
            self._session = None
    
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Dict = None,
        signed: bool = True
    ) -> Any:
        """Make API request"""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        
        url = f"{self.base_url}{endpoint}"
        params = params or {}
        
        if signed:
            params["timestamp"] = self._get_timestamp()
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
        
        if method == "GET":
            async with self._session.get(url, headers=headers, params=params) as response:
                status_code = response.status
                result = await response.json()
        else:
            async with self._session.request(method, url, headers=headers, params=params) as response:
                status_code = response.status
                result = await response.json()
        
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
    
    async def get_position(self, symbol: str) -> Optional[Position]:
        """Get position for symbol"""
        binance_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BINANCE)
        
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
        
        params = {
            "symbol": binance_symbol,
            "side": "BUY" if side == Side.LONG else "SELL",
            "type": "MARKET",
            "quantity": size,
        }
        
        if reduce_only:
            params["reduceOnly"] = "true"
        else:
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
    
    async def close_position(self, symbol: str) -> Order:
        """Close position for symbol"""
        binance_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BINANCE)
        
        position = await self.get_position(binance_symbol)
        
        if not position:
            raise Exception(f"No position found for {binance_symbol}")
        
        # Close by placing opposite order
        close_side = "SELL" if position.side == Side.LONG else "BUY"
        
        params = {
            "symbol": binance_symbol,
            "side": close_side,
            "type": "MARKET",
            "quantity": position.size,
            "positionSide": "LONG" if position.side == Side.LONG else "SHORT",
            "reduceOnly": "true"
        }
        
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
    
    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """Get current funding rate"""
        binance_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BINANCE)
        
        result = await self._request("GET", "/fapi/v1/premiumIndex", {"symbol": binance_symbol}, signed=False)
        
        return FundingRate(
            symbol=binance_symbol,
            funding_rate=float(result.get("lastFundingRate", 0)),
            next_funding_time=datetime.fromtimestamp(result.get("nextFundingTime", 0) / 1000, tz=timezone.utc),
            estimated_rate=float(result.get("interestRate", 0)),
            raw_data=result
        )
    
    async def get_mark_price(self, symbol: str) -> float:
        """Get current mark price"""
        binance_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.BINANCE)
        
        result = await self._request("GET", "/fapi/v1/premiumIndex", {"symbol": binance_symbol}, signed=False)
        
        return float(result.get("markPrice", 0))
    
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
            return True
        except Exception as e:
            # Might fail if already in the requested mode
            if "No need to change position side" in str(e):
                return True
            print(f"Error setting position mode: {e}")
            return False
    
    async def get_income_history(self, income_type: str = "FUNDING_FEE", limit: int = 100) -> List[Dict]:
        """Get income history (funding fees, etc.)"""
        result = await self._request("GET", "/fapi/v1/income", {
            "incomeType": income_type,
            "limit": limit
        })
        
        return result
    
    def get_exchange_name(self) -> str:
        """Get exchange name"""
        return "Binance"

"""
Aster Futures API Client
AsterDex uses Binance-compatible API endpoints
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

from config.settings import AsterdexConfig
from config.constants import Side, OrderType, PositionStatus, Exchange, get_exchange_symbol
from .base import BaseExchangeClient, Position, Order, FundingRate, Balance

logger = logging.getLogger(__name__)


class AsterClient(BaseExchangeClient):
    """Aster Futures API Client (Binance-compatible API)"""
    
    def __init__(self, config: AsterdexConfig, debug: bool = False):
        super().__init__(config.api_key, config.secret_key)
        self.config = config
        self.base_url = config.base_url
        self._session: Optional[aiohttp.ClientSession] = None
        self.debug = debug
        self.hedge_mode = True
        
        # Caches
        self._funding_info_cache: list = []
        self._funding_info_cache_time: float = 0
    
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
        """Connect to Aster API"""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        
        try:
            balance = await self.get_balance()
            
            # Sync position mode
            await self._sync_position_mode()
            
            return balance is not None
        except Exception as e:
            print(f"Aster connection error: {e}")
            if self._session:
                await self._session.close()
                self._session = None
            return False
    
    async def _sync_position_mode(self):
        """Sync current position mode from exchange"""
        try:
            result = await self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
            self.hedge_mode = result.get("dualSidePosition", True)
            logger.info(f"Aster position mode synced: {'Hedge' if self.hedge_mode else 'One-Way'}")
        except Exception as e:
            logger.warning(f"Failed to sync Aster position mode: {e}")
            
    async def disconnect(self):
        """Disconnect from Aster API"""
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
        
        if signed:
            params["timestamp"] = self._get_timestamp()
            params["recvWindow"] = 60000
            params["signature"] = self._sign(params)
        
        headers = self._get_headers()
        
        if self.debug:
            print(f"\n{'='*60}")
            print(f"[ASTER REQUEST]")
            print(f"  Method: {method}")
            print(f"  URL: {url}")
            print(f"  Params: {json.dumps({k: v for k, v in params.items() if k != 'signature'}, indent=2)}")
            print(f"  Headers: X-MBX-APIKEY: {self.api_key[:8]}...")
        
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
            raise Exception(f"Aster API request timeout after {timeout}s")
        
        if self.debug:
            print(f"[ASTER RESPONSE]")
            print(f"  Status: {status_code}")
            if isinstance(result, list) and len(result) > 0 and "asset" in result[0]:
                filtered = [a for a in result if a.get("asset") == "USDT" or float(a.get("balance", 0)) > 0]
                print(f"  Data (filtered): {json.dumps(filtered, indent=2)}")
            else:
                print(f"  Data: {json.dumps(result, indent=2)[:2000]}")
            print(f"{'='*60}\n")
        
        if isinstance(result, dict) and result.get("code") and result.get("code") != 200:
            raise Exception(f"Aster API Error: {result.get('msg', 'Unknown error')} (code: {result.get('code')})")
        
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
        aster_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.ASTER)
        
        result = await self._request("GET", "/fapi/v2/positionRisk", {"symbol": aster_symbol})
        
        for pos_data in result:
            if pos_data["symbol"] == aster_symbol:
                pos_amt = float(pos_data.get("positionAmt", 0))
                
                if pos_amt == 0:
                    continue
                
                side = Side.LONG if pos_amt > 0 else Side.SHORT
                
                return Position(
                    symbol=aster_symbol,
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
        aster_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.ASTER)
        
        params = {
            "symbol": aster_symbol,
            "side": "BUY" if side == Side.LONG else "SELL",
            "type": "MARKET",
            "quantity": size,
        }
        
        if reduce_only:
            params["reduceOnly"] = "true"
        elif self.hedge_mode:
            params["positionSide"] = "LONG" if side == Side.LONG else "SHORT"
        
        result = await self._request("POST", "/fapi/v1/order", params)
        
        return Order(
            order_id=str(result["orderId"]),
            symbol=aster_symbol,
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
        """Close position for symbol"""
        aster_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.ASTER)
        
        position = await self.get_position(aster_symbol)
        
        if not position:
            raise Exception(f"No position found for {aster_symbol}")
        
        close_side = "SELL" if position.side == Side.LONG else "BUY"
        
        params = {
            "symbol": aster_symbol,
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
            symbol=aster_symbol,
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
        aster_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.ASTER)
        
        position = await self.get_position(aster_symbol)
        
        if not position:
            raise Exception(f"No position found for {aster_symbol}")
        
        size = self._round_quantity(aster_symbol, size)
        close_side = "SELL" if position.side == Side.LONG else "BUY"
        
        params = {
            "symbol": aster_symbol,
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
            symbol=aster_symbol,
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
        aster_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.ASTER)
        
        try:
            await self._request("POST", "/fapi/v1/leverage", {
                "symbol": aster_symbol,
                "leverage": leverage
            })
            return True
        except Exception as e:
            print(f"Error setting leverage: {e}")
            return False
    
    async def set_position_mode(self, hedge_mode: bool = True) -> bool:
        """Set position mode (hedge or one-way)"""
        try:
            await self._request("POST", "/fapi/v1/positionSide/dual", {
                "dualSidePosition": "true" if hedge_mode else "false"
            })
            self.hedge_mode = hedge_mode
            return True
        except Exception as e:
            if "No need to change position side" in str(e) or "-4059" in str(e):
                self.hedge_mode = hedge_mode
                return True
            print(f"Error setting position mode: {e}")
            return False
    
    async def _get_funding_info_cached(self) -> list:
        """Get fundingInfo with 5-minute cache"""
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
            logger.warning(f"Aster failed to fetch funding info: {e}")
        
        return self._funding_info_cache or []
    
    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """Get current funding rate"""
        aster_symbol = symbol
        if "/" in symbol:
            aster_symbol = get_exchange_symbol(symbol, Exchange.ASTER)
        elif "-" in symbol:
            aster_symbol = symbol.replace("-", "")
             
        premium_result = await self._request("GET", "/fapi/v1/premiumIndex", {"symbol": aster_symbol}, signed=False)
        funding_info_result = await self._get_funding_info_cached()
        
        interval_hours = 8
        for item in funding_info_result:
            if item.get("symbol") == aster_symbol:
                interval_hours = item.get("fundingIntervalHours", 8)
                break
                
        next_funding_time = datetime.fromtimestamp(premium_result.get("nextFundingTime", 0) / 1000, tz=timezone.utc)
        
        return FundingRate(
            symbol=aster_symbol,
            funding_rate=float(premium_result.get("lastFundingRate", 0)),
            next_funding_time=next_funding_time,
            estimated_rate=float(premium_result.get("interestRate", 0)),
            funding_interval_hours=interval_hours,
            raw_data=premium_result
        )
    
    async def get_all_funding_rates(self) -> List[FundingRate]:
        """Get all funding rates"""
        premium_result = await self._request("GET", "/fapi/v1/premiumIndex", {}, signed=False)
        funding_info_result = await self._get_funding_info_cached()
        
        interval_map = {}
        for item in funding_info_result:
            sym = item.get("symbol")
            if sym:
                interval_map[sym] = item.get("fundingIntervalHours", 8)
                
        funding_rates = []
        for item in premium_result:
            symbol = item.get("symbol", "")
            if symbol.endswith("USDT"):
                interval_hours = interval_map.get(symbol, 8)
                
                funding_rates.append(FundingRate(
                    symbol=symbol,
                    funding_rate=float(item.get("lastFundingRate", 0)),
                    next_funding_time=datetime.fromtimestamp(item.get("nextFundingTime", 0) / 1000, tz=timezone.utc),
                    estimated_rate=float(item.get("interestRate", 0)),
                    funding_interval_hours=interval_hours,
                    raw_data=item
                ))
        
        return funding_rates
    
    async def get_mark_price(self, symbol: str) -> float:
        """Get current mark price"""
        aster_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.ASTER)
        result = await self._request("GET", "/fapi/v1/premiumIndex", {"symbol": aster_symbol}, signed=False)
        return float(result.get("markPrice", 0))
    
    async def get_order_book(self, symbol: str, limit: int = 20) -> Dict[str, Any]:
        """Get order book (depth)"""
        aster_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.ASTER)
        result = await self._request("GET", "/fapi/v1/depth", {"symbol": aster_symbol, "limit": limit}, signed=False)
        return {
            "bids": [[float(price), float(qty)] for price, qty in result["bids"]],
            "asks": [[float(price), float(qty)] for price, qty in result["asks"]]
        }
    
    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """Get ticker data"""
        aster_symbol = symbol if "USDT" in symbol and "-" not in symbol else get_exchange_symbol(symbol, Exchange.ASTER)
        result = await self._request("GET", "/fapi/v1/ticker/24hr", {"symbol": aster_symbol}, signed=False)
        
        return {
            "symbol": aster_symbol,
            "last": float(result.get("lastPrice", 0)),
            "bid": float(result.get("bidPrice", 0)),
            "ask": float(result.get("askPrice", 0)),
            "volume": float(result.get("volume", 0)),
            "raw": result
        }
    
    async def get_income_history(self, income_type: str = "FUNDING_FEE", limit: int = 100, symbol: Optional[str] = None) -> List[Dict]:
        """Get income history (funding fees, etc.)"""
        params = {
            "incomeType": income_type,
            "limit": limit
        }
        
        result = await self._request("GET", "/fapi/v1/income", params)
        
        if symbol:
            aster_symbol = symbol if "USDT" in symbol and "/" not in symbol else symbol.replace('/', '')
            result = [item for item in result if item.get('symbol') == aster_symbol]
        
        return result
    
    def get_exchange_name(self) -> str:
        """Get exchange name"""
        return "AsterDex"

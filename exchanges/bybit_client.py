"""
Bybit V5 API Client for USDT Perpetual Futures
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

from config.settings import BybitConfig
from config.constants import Side, OrderType, PositionStatus, Exchange, get_exchange_symbol
from .base import BaseExchangeClient, Position, Order, FundingRate, Balance

logger = logging.getLogger(__name__)


class BybitClient(BaseExchangeClient):
    """Bybit V5 USDT Perpetual Futures Client"""

    def __init__(self, config: BybitConfig, debug: bool = False):
        super().__init__(config.api_key, config.secret_key)
        self.config = config
        self.base_url = config.base_url
        self._session: Optional[aiohttp.ClientSession] = None
        self.debug = debug
        self.recv_window = "15000"

    # ── Connection ────────────────────────────────────────────

    async def connect(self) -> bool:
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
        if self._session:
            await self._session.close()
            await asyncio.sleep(0.25)
            self._session = None

    def get_exchange_name(self) -> str:
        return "bybit"

    # ── Authentication ────────────────────────────────────────

    def _get_timestamp(self) -> str:
        return str(int(time.time() * 1000))

    def _sign(self, timestamp: str, params_str: str) -> str:
        """HMAC-SHA256 signature: timestamp + apiKey + recvWindow + paramsStr"""
        sign_str = timestamp + self.api_key + self.recv_window + params_str
        return hmac.new(
            self.secret_key.encode("utf-8"),
            sign_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _get_headers(self, timestamp: str, sign: str) -> Dict[str, str]:
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-SIGN": sign,
            "X-BAPI-RECV-WINDOW": self.recv_window,
            "Content-Type": "application/json",
        }

    # ── HTTP helpers ──────────────────────────────────────────

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        signed: bool = True,
        timeout: float = 15.0,
    ) -> Any:
        if self._session is None:
            self._session = aiohttp.ClientSession()

        url = f"{self.base_url}{endpoint}"
        params = params or {}
        req_timeout = aiohttp.ClientTimeout(total=timeout)

        if method == "GET":
            from urllib.parse import urlencode
            query_string = urlencode(params) if params else ""
            if signed:
                ts = self._get_timestamp()
                sig = self._sign(ts, query_string)
                headers = self._get_headers(ts, sig)
            else:
                headers = {"Content-Type": "application/json"}
            async with self._session.get(
                url, headers=headers, params=params, timeout=req_timeout
            ) as resp:
                result = await resp.json()
        else:
            body_str = json.dumps(params) if params else ""
            if signed:
                ts = self._get_timestamp()
                sig = self._sign(ts, body_str)
                headers = self._get_headers(ts, sig)
            else:
                headers = {"Content-Type": "application/json"}
            async with self._session.post(
                url, headers=headers, data=body_str, timeout=req_timeout
            ) as resp:
                result = await resp.json()

        if self.debug:
            print(f"[BYBIT] {method} {endpoint} -> {json.dumps(result)[:500]}")

        ret_code = result.get("retCode", -1)
        if ret_code != 0:
            raise Exception(
                f"Bybit API Error: {result.get('retMsg', 'Unknown')} (code: {ret_code})"
            )
        return result.get("result", result)

    # ── Balance ───────────────────────────────────────────────

    async def get_balance(self, currency: str = "USDT") -> Balance:
        result = await self._request(
            "GET", "/v5/account/wallet-balance",
            {"accountType": "UNIFIED", "coin": currency},
        )
        for account in result.get("list", []):
            for coin in account.get("coin", []):
                if coin.get("coin") == currency:
                    wallet = float(coin.get("walletBalance", 0))
                    available = float(account.get("totalAvailableBalance", 0))
                    return Balance(
                        currency=currency,
                        total=wallet,
                        available=available,
                        frozen=wallet - available,
                        raw_data=coin,
                    )
        return Balance(currency=currency, total=0, available=0, frozen=0)

    # ── Positions ─────────────────────────────────────────────

    async def get_position(self, symbol: str, force_rest: bool = False) -> Optional[Position]:
        bybit_symbol = self._to_bybit_symbol(symbol)
        result = await self._request(
            "GET", "/v5/position/list",
            {"category": "linear", "symbol": bybit_symbol},
        )
        for pos in result.get("list", []):
            size = float(pos.get("size", 0))
            if size == 0:
                continue
            side_str = pos.get("side", "")
            side = Side.LONG if side_str == "Buy" else Side.SHORT
            return Position(
                symbol=bybit_symbol,
                side=side,
                size=size,
                entry_price=float(pos.get("avgPrice", 0)),
                mark_price=float(pos.get("markPrice", 0)),
                liquidation_price=float(pos.get("liqPrice", 0) or 0),
                unrealized_pnl=float(pos.get("unrealisedPnl", 0)),
                leverage=int(float(pos.get("leverage", 1))),
                status=PositionStatus.OPEN,
                timestamp=datetime.now(timezone.utc),
                raw_data=pos,
            )
        return None

    async def get_all_positions(self) -> List[Position]:
        result = await self._request(
            "GET", "/v5/position/list",
            {"category": "linear", "settleCoin": "USDT"},
        )
        positions = []
        for pos in result.get("list", []):
            size = float(pos.get("size", 0))
            if size == 0:
                continue
            side_str = pos.get("side", "")
            side = Side.LONG if side_str == "Buy" else Side.SHORT
            positions.append(Position(
                symbol=pos.get("symbol", ""),
                side=side,
                size=size,
                entry_price=float(pos.get("avgPrice", 0)),
                mark_price=float(pos.get("markPrice", 0)),
                liquidation_price=float(pos.get("liqPrice", 0) or 0),
                unrealized_pnl=float(pos.get("unrealisedPnl", 0)),
                leverage=int(float(pos.get("leverage", 1))),
                status=PositionStatus.OPEN,
                timestamp=datetime.now(timezone.utc),
                raw_data=pos,
            ))
        return positions

    # ── Orders ────────────────────────────────────────────────

    async def place_market_order(
        self, symbol: str, side: Side, size: float, reduce_only: bool = False
    ) -> Order:
        bybit_symbol = self._to_bybit_symbol(symbol)
        params = {
            "category": "linear",
            "symbol": bybit_symbol,
            "side": "Buy" if side == Side.LONG else "Sell",
            "orderType": "Market",
            "qty": str(size),
            "positionIdx": 0,  # one-way mode
        }
        if reduce_only:
            params["reduceOnly"] = True

        result = await self._request("POST", "/v5/order/create", params)
        order_id = result.get("orderId", "")

        # Bybit create-order is async acknowledgement only.
        # Fetch the actual fill info.
        await asyncio.sleep(0.3)
        fill = await self._get_order_detail(bybit_symbol, order_id)

        return Order(
            order_id=order_id,
            symbol=bybit_symbol,
            side=side,
            order_type=OrderType.MARKET,
            size=size,
            price=None,
            filled_size=float(fill.get("cumExecQty", size)),
            avg_price=float(fill.get("avgPrice", 0)),
            status=fill.get("orderStatus", "Filled"),
            timestamp=datetime.now(timezone.utc),
            raw_data=fill,
        )

    async def _get_order_detail(self, symbol: str, order_id: str) -> Dict:
        """Fetch filled order details via realtime endpoint"""
        for _ in range(5):
            try:
                result = await self._request(
                    "GET", "/v5/order/realtime",
                    {"category": "linear", "symbol": symbol, "orderId": order_id},
                )
                orders = result.get("list", [])
                if orders:
                    o = orders[0]
                    if o.get("orderStatus") in ("Filled", "PartiallyFilled", "Cancelled"):
                        return o
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return {}

    async def close_position(self, symbol: str, aggressive: bool = False) -> Order:
        bybit_symbol = self._to_bybit_symbol(symbol)
        position = await self.get_position(bybit_symbol)
        if not position:
            raise Exception(f"No position found for {bybit_symbol}")

        close_side = Side.SHORT if position.side == Side.LONG else Side.LONG
        return await self.place_market_order(
            bybit_symbol, close_side, position.size, reduce_only=True
        )

    async def close_position_partial(self, symbol: str, size: float) -> Order:
        bybit_symbol = self._to_bybit_symbol(symbol)
        position = await self.get_position(bybit_symbol)
        if not position:
            raise Exception(f"No position found for {bybit_symbol}")

        close_side = Side.SHORT if position.side == Side.LONG else Side.LONG
        return await self.place_market_order(
            bybit_symbol, close_side, size, reduce_only=True
        )

    # ── Leverage ──────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        bybit_symbol = self._to_bybit_symbol(symbol)
        try:
            await self._request("POST", "/v5/position/set-leverage", {
                "category": "linear",
                "symbol": bybit_symbol,
                "buyLeverage": str(leverage),
                "sellLeverage": str(leverage),
            })
            return True
        except Exception as e:
            # "leverage not modified" is not a real error
            if "not modified" in str(e).lower():
                return True
            print(f"Bybit set leverage error: {e}")
            return False

    # ── Market Data ───────────────────────────────────────────

    async def get_mark_price(self, symbol: str) -> float:
        bybit_symbol = self._to_bybit_symbol(symbol)
        result = await self._request(
            "GET", "/v5/market/tickers",
            {"category": "linear", "symbol": bybit_symbol},
            signed=False,
        )
        for t in result.get("list", []):
            return float(t.get("markPrice", 0))
        return 0.0

    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        bybit_symbol = self._to_bybit_symbol(symbol)
        result = await self._request(
            "GET", "/v5/market/tickers",
            {"category": "linear", "symbol": bybit_symbol},
            signed=False,
        )
        for t in result.get("list", []):
            return t
        return {}

    async def get_order_book(self, symbol: str, limit: int = 20) -> Dict[str, Any]:
        bybit_symbol = self._to_bybit_symbol(symbol)
        result = await self._request(
            "GET", "/v5/market/orderbook",
            {"category": "linear", "symbol": bybit_symbol, "limit": min(limit, 200)},
            signed=False,
        )
        return {
            "bids": [[float(p), float(q)] for p, q in result.get("b", [])],
            "asks": [[float(p), float(q)] for p, q in result.get("a", [])],
        }

    # ── Funding Rates ─────────────────────────────────────────

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        bybit_symbol = self._to_bybit_symbol(symbol)
        result = await self._request(
            "GET", "/v5/market/tickers",
            {"category": "linear", "symbol": bybit_symbol},
            signed=False,
        )
        for t in result.get("list", []):
            next_ts = int(t.get("nextFundingTime", 0))
            interval_str = t.get("fundingIntervalHour", "")
            interval_hours = int(interval_str) if interval_str else 8
            return FundingRate(
                symbol=bybit_symbol,
                funding_rate=float(t.get("fundingRate", 0)),
                next_funding_time=datetime.fromtimestamp(
                    next_ts / 1000, tz=timezone.utc
                ) if next_ts else datetime.now(timezone.utc),
                funding_interval_hours=interval_hours,
                raw_data=t,
            )
        raise Exception(f"No ticker data for {bybit_symbol}")

    async def get_all_funding_rates(self) -> List[FundingRate]:
        """Fetch all linear USDT perpetual tickers in one call"""
        result = await self._request(
            "GET", "/v5/market/tickers",
            {"category": "linear"},
            signed=False,
        )
        rates = []
        for t in result.get("list", []):
            sym = t.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            try:
                next_ts = int(t.get("nextFundingTime", 0))
                interval_str = t.get("fundingIntervalHour", "")
                interval_hours = int(interval_str) if interval_str else 8
                rates.append(FundingRate(
                    symbol=sym,
                    funding_rate=float(t.get("fundingRate", 0)),
                    next_funding_time=datetime.fromtimestamp(
                        next_ts / 1000, tz=timezone.utc
                    ) if next_ts else datetime.now(timezone.utc),
                    funding_interval_hours=interval_hours,
                    raw_data=t,
                ))
            except Exception as e:
                logger.debug(f"Error parsing Bybit ticker for {sym}: {e}")
        return rates

    # ── Closed PnL ────────────────────────────────────────────

    async def get_closed_pnl(self, symbol: str, since: Optional[int] = None) -> Dict[str, Any]:
        bybit_symbol = self._to_bybit_symbol(symbol)
        params: Dict[str, Any] = {
            "category": "linear",
            "symbol": bybit_symbol,
            "limit": 100,
        }
        if since:
            params["startTime"] = since

        result = await self._request("GET", "/v5/position/closed-pnl", params)

        total_pnl = 0.0
        total_fee = 0.0
        for item in result.get("list", []):
            total_pnl += float(item.get("closedPnl", 0))
            total_fee += float(item.get("openFee", 0)) + float(item.get("closeFee", 0))

        return {
            "realized_pnl": total_pnl,
            "commission": -abs(total_fee),
            "funding_fee": 0,  # Bybit closed-pnl doesn't include funding; use transaction log
        }

    # ── Helpers ───────────────────────────────────────────────

    def _to_bybit_symbol(self, symbol: str) -> str:
        """Normalize symbol to Bybit format (BTCUSDT)"""
        if "/" in symbol:
            return get_exchange_symbol(symbol, Exchange.BYBIT)
        # Already looks like BTCUSDT or has dash
        return symbol.replace("-", "").replace("_", "")

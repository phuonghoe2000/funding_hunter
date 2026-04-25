"""
Binance Spot API client used by the cash-carry strategy.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import aiohttp

from config.constants import Exchange, get_exchange_symbol
from config.settings import BinanceConfig
from exchanges.spot_base import BaseSpotExchangeClient, SpotBalance, SpotOrder

logger = logging.getLogger(__name__)


class BinanceSpotClient(BaseSpotExchangeClient):
    def __init__(self, config: BinanceConfig, debug: bool = False):
        super().__init__(config.api_key, config.secret_key)
        self.config = config
        self.debug = debug
        self.base_url = "https://testnet.binance.vision" if config.testnet else "https://api.binance.com"
        self._session: Optional[aiohttp.ClientSession] = None
        self._symbol_info_cache: dict[str, dict[str, Any]] = {}

    async def connect(self) -> bool:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            await self.get_balance("USDT")
            return True
        except Exception as exc:
            logger.error("Binance spot connection error: %s", exc)
            if self._session:
                await self._session.close()
                self._session = None
            return False

    async def disconnect(self) -> None:
        if self._session:
            await self._session.close()
            await asyncio.sleep(0.1)
            self._session = None

    def _get_timestamp(self) -> int:
        return int(time.time() * 1000)

    def _sign(self, params: Dict[str, Any]) -> str:
        query_string = urlencode(params)
        return hmac.new(
            self.secret_key.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _get_headers(self) -> Dict[str, str]:
        return {
            "X-MBX-APIKEY": self.api_key,
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        signed: bool = True,
        timeout: float = 15.0,
    ) -> Any:
        if self._session is None:
            self._session = aiohttp.ClientSession()

        url = f"{self.base_url}{endpoint}"
        params = dict(params or {})
        if signed:
            params["timestamp"] = self._get_timestamp()
            params.setdefault("recvWindow", 60000)
            params["signature"] = self._sign(params)

        headers = self._get_headers()
        req_timeout = aiohttp.ClientTimeout(total=timeout)

        if self.debug:
            logger.info("[BINANCE SPOT REQUEST] %s %s %s", method, url, json.dumps({k: v for k, v in params.items() if k != "signature"}))

        try:
            async with self._session.request(method, url, headers=headers, params=params, timeout=req_timeout) as response:
                status_code = response.status
                result = await response.json()
        except asyncio.TimeoutError as exc:
            raise Exception(f"Binance spot API timeout after {timeout}s") from exc

        if self.debug:
            logger.info("[BINANCE SPOT RESPONSE] %s %s", status_code, json.dumps(result)[:2000])

        if isinstance(result, dict) and "code" in result and status_code >= 400:
            raise Exception(f"Binance spot API Error: {result.get('msg', 'Unknown error')} (code: {result.get('code')})")
        return result

    @staticmethod
    def _base_asset(symbol: str) -> str:
        if symbol.endswith("USDT"):
            return symbol[:-4]
        return symbol

    async def get_balance(self, asset: str = "USDT") -> SpotBalance:
        result = await self._request("GET", "/api/v3/account")
        for item in result.get("balances", []):
            if item.get("asset") == asset:
                free = float(item.get("free", 0.0))
                locked = float(item.get("locked", 0.0))
                return SpotBalance(
                    asset=asset,
                    total=free + locked,
                    available=free,
                    frozen=locked,
                    raw_data=item,
                )
        return SpotBalance(asset=asset, total=0.0, available=0.0, frozen=0.0)

    async def get_order_book(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        spot_symbol = symbol if "/" not in symbol else get_exchange_symbol(symbol, Exchange.BINANCE)
        result = await self._request(
            "GET",
            "/api/v3/depth",
            {"symbol": spot_symbol, "limit": limit},
            signed=False,
        )
        return {
            "bids": [[float(price), float(qty)] for price, qty in result.get("bids", [])],
            "asks": [[float(price), float(qty)] for price, qty in result.get("asks", [])],
        }

    async def get_ticker(self, symbol: str) -> dict[str, Any]:
        spot_symbol = symbol if "/" not in symbol else get_exchange_symbol(symbol, Exchange.BINANCE)
        result = await self._request(
            "GET",
            "/api/v3/ticker/24hr",
            {"symbol": spot_symbol},
            signed=False,
        )
        return {
            "symbol": spot_symbol,
            "last": float(result.get("lastPrice", 0.0)),
            "bid": float(result.get("bidPrice", 0.0)),
            "ask": float(result.get("askPrice", 0.0)),
            "volume": float(result.get("volume", 0.0)),
            "quote_volume": float(result.get("quoteVolume", 0.0)),
            "raw": result,
        }

    async def get_mark_price(self, symbol: str) -> float:
        spot_symbol = symbol if "/" not in symbol else get_exchange_symbol(symbol, Exchange.BINANCE)
        result = await self._request(
            "GET",
            "/api/v3/ticker/price",
            {"symbol": spot_symbol},
            signed=False,
        )
        return float(result.get("price", 0.0))

    async def get_symbol_info(self, symbol: str) -> dict[str, Any]:
        spot_symbol = symbol if "/" not in symbol else get_exchange_symbol(symbol, Exchange.BINANCE)
        cached = self._symbol_info_cache.get(spot_symbol)
        if cached:
            return cached

        result = await self._request(
            "GET",
            "/api/v3/exchangeInfo",
            {"symbol": spot_symbol},
            signed=False,
        )
        symbols = result.get("symbols", [])
        if not symbols:
            raise Exception(f"Binance spot symbol info not found for {spot_symbol}")
        info = symbols[0]
        self._symbol_info_cache[spot_symbol] = info
        return info

    async def _round_market_quantity(self, symbol: str, quantity: float) -> float:
        info = await self.get_symbol_info(symbol)
        filters = {item["filterType"]: item for item in info.get("filters", [])}
        lot_filter = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE") or {}

        step_size = float(lot_filter.get("stepSize", 0.0) or 0.0)
        min_qty = float(lot_filter.get("minQty", 0.0) or 0.0)
        max_qty = float(lot_filter.get("maxQty", 0.0) or 0.0)

        rounded = quantity
        if step_size > 0:
            steps = math.floor(quantity / step_size)
            rounded = steps * step_size
            precision = max(0, len(str(step_size).rstrip("0").split(".")[1]) if "." in str(step_size).rstrip("0") else 0)
            rounded = round(rounded, precision)

        if min_qty > 0 and rounded < min_qty:
            raise Exception(f"Quantity {rounded} is below Binance spot minQty {min_qty}")
        if max_qty > 0 and rounded > max_qty:
            rounded = max_qty
        if rounded <= 0:
            raise Exception(f"Rounded quantity became invalid for {symbol}: {rounded}")
        return rounded

    async def place_market_order(self, symbol: str, side: str, quantity: float) -> SpotOrder:
        spot_symbol = symbol if "/" not in symbol else get_exchange_symbol(symbol, Exchange.BINANCE)
        rounded_quantity = await self._round_market_quantity(spot_symbol, quantity)
        params = {
            "symbol": spot_symbol,
            "side": side.upper(),
            "type": "MARKET",
            "quantity": rounded_quantity,
            "newOrderRespType": "RESULT",
        }
        result = await self._request("POST", "/api/v3/order", params, signed=True)
        return SpotOrder(
            order_id=str(result.get("orderId")),
            symbol=spot_symbol,
            side=side.upper(),
            size=float(result.get("origQty", rounded_quantity)),
            filled_size=float(result.get("executedQty", 0.0)),
            avg_price=float(result.get("cummulativeQuoteQty", 0.0)) / float(result.get("executedQty", 1.0) or 1.0),
            status=result.get("status", "NEW"),
            timestamp=datetime.fromtimestamp(result.get("transactTime", self._get_timestamp()) / 1000, tz=timezone.utc),
            raw_data=result,
        )

    async def get_base_asset_balance_for_pair(self, pair: str) -> SpotBalance:
        return await self.get_balance(self._base_asset(get_exchange_symbol(pair, Exchange.BINANCE)))

    def get_exchange_name(self) -> str:
        return "Binance Spot"

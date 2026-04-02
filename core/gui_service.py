import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from config.constants import Exchange, get_exchange_symbol, get_unified_pair
from config.settings import Settings
from core.multi_exchange import MultiExchangeManager
from core.opportunity import build_best_opportunity, build_directional_opportunity
from core.trading_engine import TradingEngine
from exchanges.aster_client import AsterClient
from exchanges.binance_client import BinanceClient
from exchanges.bingx_client import BingXClient
from exchanges.bybit_client import BybitClient
from exchanges.gate_client import GateClient
from exchanges.okx_client import OKXClient

logger = logging.getLogger(__name__)


class GUIWorkflowService:
    """Shared async workflows for the GUI so app.py only handles UI state."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.manager = MultiExchangeManager()
        self.engine = TradingEngine(settings)
        self.engine.exchange_manager = self.manager

    async def connect_exchanges(
        self,
        exchange_config: Dict[Exchange, Dict[str, Any]],
        *,
        debug: bool = False,
        time_sync_fn: Optional[Callable[[], Tuple[bool, str]]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> Tuple[List[str], Dict[Exchange, Any]]:
        if time_sync_fn:
            success, message = time_sync_fn()
            if log_callback:
                log_callback(message)
            elif not success:
                logger.warning(message)

        exchange_specs = [
            (
                Exchange.OKX,
                "OKX",
                self.settings.okx,
                OKXClient,
                ("api_key", "secret_key", "passphrase"),
            ),
            (
                Exchange.BINANCE,
                "Binance",
                self.settings.binance,
                BinanceClient,
                ("api_key", "secret_key"),
            ),
            (
                Exchange.BINGX,
                "BingX",
                self.settings.bingx,
                BingXClient,
                ("api_key", "secret_key"),
            ),
            (
                Exchange.GATE,
                "Gate.io",
                self.settings.gate,
                GateClient,
                ("api_key", "secret_key"),
            ),
            (
                Exchange.ASTERDEX,
                "Asterdex",
                self.settings.asterdex,
                AsterClient,
                ("api_key", "secret_key"),
            ),
            (
                Exchange.BYBIT,
                "Bybit",
                self.settings.bybit,
                BybitClient,
                ("api_key", "secret_key"),
            ),
        ]

        connected: List[str] = []

        for exchange, display_name, settings_obj, client_cls, required_fields in exchange_specs:
            config = exchange_config.get(exchange, {})
            if not config.get("enabled"):
                continue

            for field_name in required_fields:
                value = config.get(field_name)
                if value is not None:
                    setattr(settings_obj, field_name, value)

            if "testnet" in config:
                settings_obj.testnet = bool(config["testnet"])

            if not all(getattr(settings_obj, field, "") for field in required_fields):
                continue

            client = client_cls(settings_obj, debug=debug)
            if await self.manager.connect_exchange(exchange, client):
                connected.append(display_name)

        balances = await self.manager.get_all_balances()
        return connected, balances

    async def scan_funding_rates(self, top_candidates: int = 20) -> Dict[str, Dict[Exchange, Any]]:
        """
        Fetch all funding rates using bulk endpoints first, then enrich the top pairs on slower exchanges.
        Mirrors the previous GUI workflow but keeps the orchestration out of app.py.
        """
        bulk_tasks = {}

        for exchange in [Exchange.BINANCE, Exchange.ASTERDEX, Exchange.BINGX, Exchange.BYBIT]:
            client = self.manager.clients.get(exchange)
            if client and hasattr(client, "get_all_funding_rates"):
                bulk_tasks[exchange] = client.get_all_funding_rates()

        if not bulk_tasks:
            return {}

        exchanges_list = list(bulk_tasks.keys())
        results = await asyncio.gather(*bulk_tasks.values(), return_exceptions=True)

        all_rates: Dict[str, Dict[Exchange, Any]] = {}
        for exchange, result in zip(exchanges_list, results):
            if isinstance(result, Exception):
                logger.warning(f"Failed to fetch {exchange.value} rates: {result}")
                continue

            for rate_obj in result:
                pair = get_unified_pair(getattr(rate_obj, "symbol", ""), exchange)
                all_rates.setdefault(pair, {})[exchange] = rate_obj

        ranked_pairs = []
        for pair, rates in all_rates.items():
            metrics = build_best_opportunity(pair, rates)
            net_edge = metrics.net_edge_pct if metrics else float("-inf")
            ranked_pairs.append((pair, net_edge))
        ranked_pairs.sort(key=lambda item: item[1], reverse=True)
        top_pairs = [pair for pair, _ in ranked_pairs[:top_candidates]]

        enrich_tasks = []
        for exchange in [Exchange.OKX, Exchange.GATE]:
            client = self.manager.clients.get(exchange)
            if not client:
                continue
            for pair in top_pairs:
                symbol = get_exchange_symbol(pair, exchange)
                enrich_tasks.append(self._fetch_single_funding_rate(client, exchange, pair, symbol))

        if enrich_tasks:
            enrich_results = await asyncio.gather(*enrich_tasks, return_exceptions=True)
            for item in enrich_results:
                if isinstance(item, Exception) or item is None:
                    continue
                pair, exchange, rate_obj = item
                if rate_obj:
                    all_rates.setdefault(pair, {})[exchange] = rate_obj

        return all_rates

    async def get_pair_snapshot(
        self,
        pair: str,
        long_exchange: Exchange,
        short_exchange: Exchange,
    ) -> Dict[str, Any]:
        result_data = {}
        funding_rates = {}

        tasks = []
        for exchange in [long_exchange, short_exchange]:
            client = self.manager.clients.get(exchange)
            if client:
                symbol = get_exchange_symbol(pair, exchange)
                tasks.append(self._fetch_exchange_data(client, exchange, symbol))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, dict):
                    exchange = result["exchange"]
                    result_data[exchange] = result
                    if "funding_rate" in result:
                        funding_rates[exchange] = result["funding_rate"]

        snapshot = {
            "pair": pair,
            "long_exchange": long_exchange,
            "short_exchange": short_exchange,
            "result_data": result_data,
            "funding_rates": funding_rates,
            "selected_trade": None,
            "recommended_trade": None,
        }

        long_data = result_data.get(long_exchange, {})
        short_data = result_data.get(short_exchange, {})
        long_book = long_data.get("order_book") or {}
        short_book = short_data.get("order_book") or {}

        long_ask = self._first_book_price(long_book.get("asks"))
        short_bid = self._first_book_price(short_book.get("bids"))
        long_bid = self._first_book_price(long_book.get("bids"))
        short_ask = self._first_book_price(short_book.get("asks"))

        if long_ask and short_bid:
            avg = (long_ask + short_bid) / 2
            snapshot["open_spread_pct"] = ((short_bid - long_ask) / avg) * 100
            snapshot["price_diff"] = short_bid - long_ask
            snapshot["long_entry_price"] = long_ask
            snapshot["short_entry_price"] = short_bid

        if long_bid and short_ask:
            avg_close = (long_bid + short_ask) / 2
            snapshot["close_spread_pct"] = ((long_bid - short_ask) / avg_close) * 100

        if long_exchange in funding_rates and short_exchange in funding_rates:
            snapshot["selected_trade"] = build_directional_opportunity(
                pair=pair,
                long_exchange=long_exchange,
                short_exchange=short_exchange,
                long_rate_obj=funding_rates[long_exchange],
                short_rate_obj=funding_rates[short_exchange],
            ).to_dict()

        if len(funding_rates) >= 2:
            metrics = build_best_opportunity(pair, funding_rates)
            if metrics:
                snapshot["recommended_trade"] = metrics.to_dict()

        next_funding_time = None
        if funding_rates.get(long_exchange) and funding_rates[long_exchange].next_funding_time:
            next_funding_time = funding_rates[long_exchange].next_funding_time
        elif funding_rates.get(short_exchange) and funding_rates[short_exchange].next_funding_time:
            next_funding_time = funding_rates[short_exchange].next_funding_time

        now = datetime.now(timezone.utc)
        if not next_funding_time:
            current_hour = now.hour
            for funding_hour in [0, 8, 16]:
                if current_hour < funding_hour:
                    next_funding_time = now.replace(hour=funding_hour, minute=0, second=0, microsecond=0)
                    break
            if not next_funding_time:
                next_funding_time = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

        if next_funding_time:
            time_until = next_funding_time - now
            total_seconds = max(0, int(time_until.total_seconds()))
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            seconds = total_seconds % 60
            snapshot["next_funding_time"] = next_funding_time
            snapshot["time_until_funding"] = time_until
            snapshot["time_until_funding_text"] = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            snapshot["hours_until_funding"] = total_seconds / 3600

        return snapshot

    async def get_initial_position_state(
        self,
        pair: str,
        long_exchange: Exchange,
        short_exchange: Exchange,
        size: float,
    ) -> Dict[str, Any]:
        rates = {}
        for exchange in [long_exchange, short_exchange]:
            client = self.manager.clients.get(exchange)
            if client:
                symbol = get_exchange_symbol(pair, exchange)
                try:
                    funding_rate = await client.get_funding_rate(symbol)
                    rates[exchange] = funding_rate.funding_rate
                except Exception as exc:
                    logger.debug(f"Could not get funding rate from {exchange.value}: {exc}")

        long_rate = rates.get(long_exchange, 0.0)
        short_rate = rates.get(short_exchange, 0.0)
        return {
            "pair": pair,
            "long_exchange": long_exchange,
            "short_exchange": short_exchange,
            "size": size,
            "open_time": datetime.now(timezone.utc),
            "total_funding_fees": 0.0,
            "last_funding_check": datetime.now(timezone.utc),
            "initial_long_rate": long_rate,
            "initial_short_rate": short_rate,
            "initial_net_funding": short_rate - long_rate,
        }

    async def check_open_spread(
        self,
        pair: str,
        long_exchange: Exchange,
        short_exchange: Exchange,
    ) -> Dict[str, float]:
        long_client = self.manager.clients.get(long_exchange)
        short_client = self.manager.clients.get(short_exchange)
        if not long_client or not short_client:
            raise RuntimeError("Exchange clients not available")

        long_symbol = get_exchange_symbol(pair, long_exchange)
        short_symbol = get_exchange_symbol(pair, short_exchange)

        long_book, short_book = await asyncio.wait_for(
            asyncio.gather(
                long_client.get_order_book(long_symbol, limit=10),
                short_client.get_order_book(short_symbol, limit=10),
            ),
            timeout=10.0,
        )

        long_ask = self._first_book_price(long_book.get("asks"))
        short_bid = self._first_book_price(short_book.get("bids"))
        if long_ask is None or short_bid is None:
            raise RuntimeError("Order book is missing best ask or best bid")

        avg_price = (long_ask + short_bid) / 2
        spread_pct = ((short_bid - long_ask) / avg_price) * 100
        return {
            "long_ask_price": long_ask,
            "short_bid_price": short_bid,
            "price_spread_pct": spread_pct,
        }

    async def execute_open_splits(
        self,
        *,
        pair: str,
        long_exchange: Exchange,
        short_exchange: Exchange,
        size: float,
        leverage: int,
        split_count: int,
        price_spread_min: float,
        log_callback: Optional[Callable[[str], None]] = None,
        on_first_split_complete: Optional[Callable[[float], None]] = None,
        cancel_event=None,
        skip_leverage_set: bool = False,
        skip_spread_check: bool = False,
    ) -> Dict[str, Any]:
        balance_before = {}
        for exchange in [long_exchange, short_exchange]:
            client = self.manager.clients.get(exchange)
            if client:
                try:
                    balance = await client.get_balance()
                    balance_before[exchange] = balance.available if balance else 0.0
                except Exception:
                    balance_before[exchange] = 0.0

        result = await self.manager.open_hedged_position_split(
            pair,
            long_exchange,
            short_exchange,
            size,
            leverage,
            split_count=split_count,
            delay_between_splits=5.0,
            price_spread_min=price_spread_min,
            spread_check_interval=2.0,
            max_wait_per_split=3600.0,
            log_callback=log_callback,
            on_first_split_complete=on_first_split_complete,
            skip_leverage_set=skip_leverage_set,
            cancel_event=cancel_event,
            skip_spread_check=skip_spread_check,
        )
        result["balance_before"] = balance_before
        return result

    async def build_monitor_snapshot(self, position: Dict[str, Any]) -> Dict[str, Any]:
        total_balance = 0.0
        for _, client in self.manager.clients.items():
            try:
                balance = await client.get_balance()
                if balance:
                    total_balance += balance.available
            except Exception:
                continue

        if total_balance <= 0:
            total_balance = 1000.0

        result = await self.manager.check_positions(
            position["pair"],
            position["long_exchange"],
            position["short_exchange"],
        )
        long_pos = result.get("long")
        short_pos = result.get("short")
        long_pnl = long_pos.unrealized_pnl if long_pos else 0.0
        short_pnl = short_pos.unrealized_pnl if short_pos else 0.0
        losing_pnl = (abs(long_pnl) if long_pnl < 0 else 0.0) + (abs(short_pnl) if short_pnl < 0 else 0.0)
        risk_percent = (losing_pnl / total_balance) * 100

        return {
            "total_balance": total_balance,
            "long_pnl": long_pnl,
            "short_pnl": short_pnl,
            "risk_percent": risk_percent,
            "check_result": result,
        }

    async def close_position_with_analysis(
        self,
        *,
        pair: str,
        long_exchange: Exchange,
        short_exchange: Exchange,
        splits: int,
        log_callback: Optional[Callable[[str], None]] = None,
        cancel_event=None,
        skip_spread_check: bool = False,
        close_size: Optional[float] = None,
    ) -> Dict[str, Any]:
        threshold = -100.0
        analyze_result = None

        if not skip_spread_check:
            analyze_result = await self.manager.analyze_spread(
                pair,
                long_exchange,
                short_exchange,
                duration_seconds=120.0,
                check_interval=2.0,
                log_callback=log_callback,
                cancel_event=cancel_event,
                mode="close",
            )
            if analyze_result.get("success"):
                threshold = analyze_result["second_best_spread"]
                if log_callback:
                    log_callback(f"✅ Analyze done. Threshold = {threshold:.4f}%")
            elif log_callback:
                log_callback(f"Analyze failed: {analyze_result.get('error')}. Falling back to immediate close threshold.")

        if log_callback:
            log_callback(f"🔄 Closing position in {splits} split(s)...")

        result = await self.manager.close_hedged_position_split(
            pair,
            long_exchange,
            short_exchange,
            splits=splits,
            interval_seconds=2.0,
            price_spread_min=threshold,
            spread_check_interval=2.0,
            progress_callback=(lambda split_num, total_splits, message: log_callback(f"   {message}")) if log_callback else None,
            cancel_event=cancel_event,
            skip_spread_check=skip_spread_check,
            close_size=close_size,
        )
        result["analyze_result"] = analyze_result
        result["price_spread_min"] = threshold
        return result

    async def _fetch_single_funding_rate(self, client, exchange: Exchange, pair: str, symbol: str):
        try:
            rate_obj = await asyncio.wait_for(client.get_funding_rate(symbol), timeout=8.0)
            return pair, exchange, rate_obj
        except Exception:
            return pair, exchange, None

    async def _fetch_exchange_data(self, client, exchange: Exchange, symbol: str) -> Dict[str, Any]:
        result = {"exchange": exchange}

        try:
            result["order_book"] = await asyncio.wait_for(client.get_order_book(symbol, limit=5), timeout=8.0)
        except Exception as exc:
            logger.debug(f"Could not get order book from {exchange.value}: {exc}")

        try:
            result["price"] = await asyncio.wait_for(client.get_mark_price(symbol), timeout=8.0)
        except Exception as exc:
            logger.debug(f"Could not get price from {exchange.value}: {exc}")

        try:
            result["funding_rate"] = await asyncio.wait_for(client.get_funding_rate(symbol), timeout=8.0)
        except Exception as exc:
            logger.debug(f"Could not get funding rate from {exchange.value}: {exc}")

        return result

    @staticmethod
    def _first_book_price(levels) -> Optional[float]:
        if not levels:
            return None
        return float(levels[0][0])

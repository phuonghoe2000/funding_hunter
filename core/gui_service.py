import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from config.constants import Exchange, get_exchange_symbol, get_unified_pair
from config.settings import Settings
from core.multi_exchange import MultiExchangeManager
from core.opportunity import build_best_opportunity, build_directional_opportunity
from core.session_store import SessionStore
from core.trade_advisor import (
    build_liquidation_context,
    build_liquidation_reduce_policy,
    build_monitor_advice,
    build_trade_plan,
)
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
        self.engine.cash_carry_engine.attach_future_manager(self.manager)
        self.cash_carry_engine = self.engine.cash_carry_engine
        self.session_store = SessionStore()

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
        enabled_exchanges = {exchange for exchange, config in exchange_config.items() if config.get("enabled")}

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
        try:
            await self.cash_carry_engine.connect_enabled_spot_clients(
                enabled_exchanges=enabled_exchanges,
                debug=debug,
            )
        except Exception as exc:
            if log_callback:
                log_callback(f"Spot client connection warning: {exc}")
        return connected, balances

    def new_session_id(self) -> str:
        return self.session_store.new_session_id()

    def persist_active_position(self, position: Dict[str, Any]) -> None:
        self.session_store.save_active_session(position)

    def clear_active_position(self) -> None:
        self.session_store.clear_active_session()

    def record_trade_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        self.session_store.append_journal_event(event_type, payload)

    def read_active_session(self) -> Optional[Dict[str, Any]]:
        return self.session_store.load_active_session()

    def read_recent_journal(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.session_store.read_recent_journal(limit)

    def _restore_position_types(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        restored = dict(payload)
        for field in ("long_exchange", "short_exchange"):
            value = restored.get(field)
            if isinstance(value, str):
                restored[field] = Exchange(value)
        for field in ("open_time", "last_funding_check", "saved_at"):
            value = restored.get(field)
            if isinstance(value, str):
                try:
                    restored[field] = datetime.fromisoformat(value)
                except ValueError:
                    pass
        return restored

    async def recover_active_session(self) -> Optional[Dict[str, Any]]:
        payload = self.session_store.load_active_session()
        if not payload:
            return None

        if payload.get("strategy_type") == "cash_carry":
            return await self.cash_carry_engine.recover_active_session(payload)

        restored = self._restore_position_types(payload)
        pair = restored.get("pair")
        long_exchange = restored.get("long_exchange")
        short_exchange = restored.get("short_exchange")
        if not pair or not long_exchange or not short_exchange:
            return None

        check_result = await self.manager.check_positions(pair, long_exchange, short_exchange)
        long_position = check_result.get("long")
        short_position = check_result.get("short")
        if not long_position and not short_position:
            self.session_store.clear_active_session()
            self.session_store.append_journal_event(
                "session_stale",
                {"pair": pair, "long_exchange": long_exchange, "short_exchange": short_exchange},
            )
            return None

        if long_position and short_position:
            restored["long_size"] = getattr(long_position, "size", restored.get("long_size", 0.0))
            restored["short_size"] = getattr(short_position, "size", restored.get("short_size", 0.0))
            restored["size"] = min(
                value for value in [restored["long_size"], restored["short_size"]] if value is not None and value > 0
            ) if restored.get("long_size") and restored.get("short_size") else restored.get("size", 0.0)

        return restored

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
        *,
        strategy_mode: str = "futures_hedge",
    ) -> Dict[str, Any]:
        if strategy_mode == "cash_carry":
            return await self.cash_carry_engine.get_pair_snapshot(pair, long_exchange, short_exchange)

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

    def build_trade_plan_from_snapshot(
        self,
        snapshot: Dict[str, Any],
        *,
        requested_size_tokens: float,
        leverage: int,
        balances: Optional[Dict[Exchange, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if snapshot.get("strategy_type") == "cash_carry":
            return self.cash_carry_engine.build_trade_plan_from_snapshot(
                snapshot,
                requested_size_tokens=requested_size_tokens,
                leverage=leverage,
            ).to_dict()

        funding_rates = snapshot.get("funding_rates", {})
        long_exchange = snapshot.get("long_exchange")
        short_exchange = snapshot.get("short_exchange")
        if long_exchange not in funding_rates or short_exchange not in funding_rates:
            return None

        result_data = snapshot.get("result_data", {})
        long_data = result_data.get(long_exchange, {})
        short_data = result_data.get(short_exchange, {})
        long_balance = balances.get(long_exchange) if balances else None
        short_balance = balances.get(short_exchange) if balances else None

        plan = build_trade_plan(
            pair=snapshot["pair"],
            long_exchange=long_exchange,
            short_exchange=short_exchange,
            long_rate_obj=funding_rates[long_exchange],
            short_rate_obj=funding_rates[short_exchange],
            long_order_book=long_data.get("order_book"),
            short_order_book=short_data.get("order_book"),
            long_ticker=long_data.get("ticker"),
            short_ticker=short_data.get("ticker"),
            long_balance=long_balance,
            short_balance=short_balance,
            requested_size_tokens=requested_size_tokens,
            leverage=leverage,
            long_price=snapshot.get("long_entry_price", long_data.get("price")),
            short_price=snapshot.get("short_entry_price", short_data.get("price")),
        )
        return plan.to_dict()

    async def build_open_assessment(
        self,
        *,
        pair: str,
        long_exchange: Exchange,
        short_exchange: Exchange,
        requested_size_tokens: float,
        leverage: int,
        balances: Optional[Dict[Exchange, Any]] = None,
        strategy_mode: str = "futures_hedge",
    ) -> Dict[str, Any]:
        if strategy_mode == "cash_carry":
            return await self.cash_carry_engine.build_open_assessment(
                pair=pair,
                spot_exchange=long_exchange,
                future_exchange=short_exchange,
                requested_size_tokens=requested_size_tokens,
                leverage=leverage,
            )

        snapshot = await self.get_pair_snapshot(pair, long_exchange, short_exchange)
        live_balances = balances or await self.get_balances_subset(long_exchange, short_exchange)
        trade_plan = self.build_trade_plan_from_snapshot(
            snapshot,
            requested_size_tokens=requested_size_tokens,
            leverage=leverage,
            balances=live_balances,
        )
        if trade_plan:
            snapshot["trade_plan"] = trade_plan
        return {
            "snapshot": snapshot,
            "trade_plan": trade_plan,
            "balances": live_balances,
        }

    async def preflight_open_execution(
        self,
        *,
        pair: str,
        long_exchange: Exchange,
        short_exchange: Exchange,
        requested_size_tokens: float,
        leverage: int,
        strategy_mode: str = "futures_hedge",
    ) -> Dict[str, Any]:
        if strategy_mode == "cash_carry":
            return await self.cash_carry_engine.preflight_open_execution(
                pair=pair,
                spot_exchange=long_exchange,
                future_exchange=short_exchange,
                requested_size_tokens=requested_size_tokens,
                leverage=leverage,
            )

        blockers: List[str] = []
        warnings: List[str] = []
        details: Dict[str, Any] = {
            "pair": pair,
            "long_exchange": long_exchange,
            "short_exchange": short_exchange,
            "requested_size_tokens": requested_size_tokens,
            "leverage": leverage,
        }

        if requested_size_tokens <= 0:
            blockers.append("Requested size must be greater than zero.")

        long_client = self.manager.clients.get(long_exchange)
        short_client = self.manager.clients.get(short_exchange)
        if not long_client:
            blockers.append(f"{long_exchange.value} is not connected.")
        if not short_client:
            blockers.append(f"{short_exchange.value} is not connected.")

        if not blockers:
            try:
                spread_snapshot = await self.check_open_spread(pair, long_exchange, short_exchange)
                details["spread_snapshot"] = spread_snapshot
            except Exception as exc:
                blockers.append(f"Order book preflight failed: {exc}")

            try:
                balances = await self.get_balances_subset(long_exchange, short_exchange)
                details["balances"] = balances
                missing_balances = [exchange.value for exchange in [long_exchange, short_exchange] if exchange not in balances]
                if missing_balances:
                    blockers.append(f"Balance check failed for: {', '.join(missing_balances)}")
            except Exception as exc:
                blockers.append(f"Balance preflight failed: {exc}")

        return {
            "ready": not blockers,
            "blockers": blockers,
            "warnings": warnings,
            "details": details,
        }

    async def preflight_close_execution(
        self,
        *,
        pair: str,
        long_exchange: Exchange,
        short_exchange: Exchange,
        close_size: Optional[float] = None,
    ) -> Dict[str, Any]:
        blockers: List[str] = []
        warnings: List[str] = []
        details: Dict[str, Any] = {
            "pair": pair,
            "long_exchange": long_exchange,
            "short_exchange": short_exchange,
            "close_size": close_size,
        }

        long_client = self.manager.clients.get(long_exchange)
        short_client = self.manager.clients.get(short_exchange)
        if not long_client:
            blockers.append(f"{long_exchange.value} is not connected.")
        if not short_client:
            blockers.append(f"{short_exchange.value} is not connected.")

        if not blockers:
            check_result = await self.manager.check_positions(pair, long_exchange, short_exchange)
            details["check_result"] = check_result
            long_position = check_result.get("long")
            short_position = check_result.get("short")
            long_size = float(getattr(long_position, "size", 0.0) or 0.0)
            short_size = float(getattr(short_position, "size", 0.0) or 0.0)

            if long_size <= 0 and short_size <= 0:
                blockers.append("No active positions found to close.")
            elif close_size is not None:
                max_closeable = max(long_size, short_size)
                if max_closeable > 0 and close_size > max_closeable:
                    warnings.append(
                        f"Requested close size {close_size:.6f} exceeds current size {max_closeable:.6f}; close will be capped."
                    )

            try:
                long_symbol = get_exchange_symbol(pair, long_exchange)
                short_symbol = get_exchange_symbol(pair, short_exchange)
                long_book, short_book = await asyncio.wait_for(
                    asyncio.gather(
                        long_client.get_order_book(long_symbol, limit=10),
                        short_client.get_order_book(short_symbol, limit=10),
                    ),
                    timeout=10.0,
                )
                if not long_book.get("bids"):
                    blockers.append(f"{long_exchange.value} order book has no bids for close.")
                if not short_book.get("asks"):
                    blockers.append(f"{short_exchange.value} order book has no asks for close.")
                details["order_books_ok"] = not blockers
            except Exception as exc:
                blockers.append(f"Order book preflight failed: {exc}")

        return {
            "ready": not blockers,
            "blockers": blockers,
            "warnings": warnings,
            "details": details,
        }

    async def refresh_position_state(self, position: Dict[str, Any]) -> Dict[str, Any]:
        check_result = await self.manager.check_positions(
            position["pair"],
            position["long_exchange"],
            position["short_exchange"],
        )
        long_position = check_result.get("long")
        short_position = check_result.get("short")
        long_size = float(getattr(long_position, "size", 0.0) or 0.0)
        short_size = float(getattr(short_position, "size", 0.0) or 0.0)
        nonzero_sizes = [size for size in [long_size, short_size] if size > 0]

        refreshed = dict(position)
        refreshed["long_size"] = long_size
        refreshed["short_size"] = short_size
        refreshed["size"] = min(nonzero_sizes) if len(nonzero_sizes) >= 2 else (nonzero_sizes[0] if nonzero_sizes else 0.0)
        refreshed["one_side_missing"] = check_result.get("one_side_missing")
        refreshed["last_position_refresh"] = datetime.now(timezone.utc)

        return {
            "has_position": bool(nonzero_sizes),
            "position": refreshed,
            "check_result": check_result,
        }

    async def get_initial_position_state(
        self,
        pair: str,
        long_exchange: Exchange,
        short_exchange: Exchange,
        size: float,
        *,
        leverage: int = 1,
        pretrade_assessment: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        open_time: Optional[datetime] = None,
        strategy_mode: str = "futures_hedge",
    ) -> Dict[str, Any]:
        if strategy_mode == "cash_carry":
            return await self.cash_carry_engine.get_initial_position_state(
                pair=pair,
                spot_exchange=long_exchange,
                future_exchange=short_exchange,
                size=size,
                leverage=leverage,
                pretrade_assessment=pretrade_assessment,
                session_id=session_id,
                open_time=open_time,
            )

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
        state = {
            "pair": pair,
            "long_exchange": long_exchange,
            "short_exchange": short_exchange,
            "size": size,
            "leverage": leverage,
            "open_time": open_time or datetime.now(timezone.utc),
            "total_funding_fees": 0.0,
            "last_funding_check": datetime.now(timezone.utc),
            "initial_long_rate": long_rate,
            "initial_short_rate": short_rate,
            "initial_net_funding": short_rate - long_rate,
            "session_id": session_id or self.new_session_id(),
        }
        if pretrade_assessment:
            state["pretrade_assessment"] = pretrade_assessment
            state["initial_net_edge_pct"] = pretrade_assessment.get("net_edge_pct", 0.0)
            state["initial_quality_score"] = pretrade_assessment.get("quality_score", 0.0)
        return state

    async def get_balances_subset(self, *exchanges: Exchange) -> Dict[Exchange, Any]:
        balances: Dict[Exchange, Any] = {}
        for exchange in exchanges:
            client = self.manager.clients.get(exchange)
            if not client:
                continue
            try:
                balance = await client.get_balance()
                if balance:
                    balances[exchange] = balance
            except Exception as exc:
                logger.debug(f"Could not get balance from {exchange.value}: {exc}")
        return balances

    async def check_open_spread(
        self,
        pair: str,
        long_exchange: Exchange,
        short_exchange: Exchange,
        *,
        strategy_mode: str = "futures_hedge",
    ) -> Dict[str, float]:
        if strategy_mode == "cash_carry":
            return await self.cash_carry_engine.check_open_basis(pair, long_exchange, short_exchange)

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
        split_interval_seconds: float = 2.0,
        strategy_mode: str = "futures_hedge",
    ) -> Dict[str, Any]:
        if strategy_mode == "cash_carry":
            return await self.cash_carry_engine.execute_open_splits(
                pair=pair,
                spot_exchange=long_exchange,
                future_exchange=short_exchange,
                size=size,
                leverage=leverage,
                split_count=split_count,
                basis_threshold_pct=price_spread_min,
                log_callback=log_callback,
                cancel_event=cancel_event,
                skip_leverage_set=skip_leverage_set,
                skip_basis_check=skip_spread_check,
                split_interval_seconds=split_interval_seconds,
            )

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
            delay_between_splits=split_interval_seconds,
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
        if position.get("strategy_type") == "cash_carry":
            return await self.cash_carry_engine.build_monitor_snapshot(position)

        total_balance = 0.0
        balances: Dict[Exchange, Any] = {}
        for exchange, client in self.manager.clients.items():
            try:
                balance = await client.get_balance()
                if balance:
                    balances[exchange] = balance
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

        snapshot = await self.get_pair_snapshot(
            position["pair"],
            position["long_exchange"],
            position["short_exchange"],
        )
        trade_plan = self.build_trade_plan_from_snapshot(
            snapshot,
            requested_size_tokens=float(position.get("size", 0.0) or 0.0),
            leverage=int(position.get("leverage", 1) or 1),
            balances=balances,
        )
        advice = build_monitor_advice(
            position=position,
            trade_plan=trade_plan,
            risk_percent=risk_percent,
            check_result=result,
        )
        liquidation_context = build_liquidation_context(position=position, check_result=result)
        liquidation_policy = build_liquidation_reduce_policy(
            distance_pct=liquidation_context.get("min_liquidation_distance_pct"),
            activation_threshold_pct=3.0,
        )

        return {
            "total_balance": total_balance,
            "balances": balances,
            "long_pnl": long_pnl,
            "short_pnl": short_pnl,
            "risk_percent": risk_percent,
            "liquidation": liquidation_context,
            "liquidation_policy": liquidation_policy.to_dict() if liquidation_policy else None,
            "min_liquidation_distance_pct": liquidation_context.get("min_liquidation_distance_pct"),
            "check_result": result,
            "pair_snapshot": snapshot,
            "trade_plan": trade_plan,
            "monitor_advice": advice.to_dict(),
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
        split_interval_seconds: float = 2.0,
        strategy_mode: str = "futures_hedge",
    ) -> Dict[str, Any]:
        if strategy_mode == "cash_carry":
            return await self.cash_carry_engine.close_position_with_analysis(
                pair=pair,
                spot_exchange=long_exchange,
                future_exchange=short_exchange,
                splits=splits,
                log_callback=log_callback,
                cancel_event=cancel_event,
                skip_basis_check=skip_spread_check,
                close_size=close_size,
                split_interval_seconds=split_interval_seconds,
            )

        threshold = -100.0
        analyze_result = None
        preflight_result = None

        if log_callback:
            log_callback("Running close preflight checks...")
        preflight_result = await self.preflight_close_execution(
            pair=pair,
            long_exchange=long_exchange,
            short_exchange=short_exchange,
            close_size=close_size,
        )
        if not preflight_result.get("ready"):
            error_message = " | ".join(preflight_result.get("blockers", [])) or "Close preflight failed."
            if log_callback:
                log_callback(f"Close preflight failed: {error_message}")
            return {
                "success": False,
                "error": error_message,
                "preflight_result": preflight_result,
                "fully_closed": False,
                "remaining_position_state": await self.refresh_position_state(
                    {
                        "pair": pair,
                        "long_exchange": long_exchange,
                        "short_exchange": short_exchange,
                    }
                ),
            }

        if log_callback:
            for warning in preflight_result.get("warnings", []):
                log_callback(f"Close preflight warning: {warning}")

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
                threshold = analyze_result["avg_spread"]
                if log_callback:
                    log_callback(f"✅ Analyze done. Threshold (avg spread) = {threshold:.4f}%")
            elif log_callback:
                log_callback(f"Analyze failed: {analyze_result.get('error')}. Falling back to immediate close threshold.")

        if log_callback:
            log_callback(f"🔄 Closing position in {splits} split(s)...")

        result = await self.manager.close_hedged_position_split(
            pair,
            long_exchange,
            short_exchange,
            splits=splits,
            interval_seconds=split_interval_seconds,
            price_spread_min=threshold,
            spread_check_interval=2.0,
            progress_callback=(lambda split_num, total_splits, message: log_callback(f"   {message}")) if log_callback else None,
            cancel_event=cancel_event,
            skip_spread_check=skip_spread_check,
            close_size=close_size,
        )
        result["analyze_result"] = analyze_result
        result["price_spread_min"] = threshold
        result["preflight_result"] = preflight_result
        try:
            remaining_position_state = await self.refresh_position_state(
                {
                    "pair": pair,
                    "long_exchange": long_exchange,
                    "short_exchange": short_exchange,
                }
            )
        except Exception as exc:
            remaining_position_state = {
                "has_position": True,
                "position": {
                    "pair": pair,
                    "long_exchange": long_exchange,
                    "short_exchange": short_exchange,
                },
                "check_result": {"error": str(exc)},
            }
        result["remaining_position_state"] = remaining_position_state
        result["fully_closed"] = not remaining_position_state.get("has_position", True)
        return result

    async def reduce_position_on_risk(
        self,
        *,
        pair: str,
        long_exchange: Exchange,
        short_exchange: Exchange,
        risk_percent: Optional[float] = None,
        threshold_percent: Optional[float] = None,
        reason: str = "risk_threshold",
        reduce_ratio: Optional[float] = None,
        splits: Optional[int] = None,
        interval_seconds: Optional[float] = None,
        policy_mode: Optional[str] = None,
        action_label: Optional[str] = None,
        strategy_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        if strategy_type == "cash_carry":
            return await self.cash_carry_engine.reduce_position_on_risk(
                pair=pair,
                spot_exchange=long_exchange,
                future_exchange=short_exchange,
                risk_percent=risk_percent,
                threshold_percent=threshold_percent,
                reason=reason,
                reduce_ratio=reduce_ratio if reduce_ratio is not None else 0.5,
                splits=splits if splits is not None else 10,
                interval_seconds=interval_seconds if interval_seconds is not None else 2.0,
                policy_mode=policy_mode or "risk_reduce",
                action_label=action_label or "REDUCE_50",
            )

        kwargs: Dict[str, Any] = {
            "risk_percent": risk_percent,
            "threshold_percent": threshold_percent,
            "reason": reason,
        }
        if reduce_ratio is not None:
            kwargs["reduce_ratio"] = reduce_ratio
        if splits is not None:
            kwargs["splits"] = splits
        if interval_seconds is not None:
            kwargs["interval_seconds"] = interval_seconds
        if policy_mode is not None:
            kwargs["policy_mode"] = policy_mode
        if action_label is not None:
            kwargs["action_label"] = action_label

        return await self.engine.reduce_position_on_risk(
            pair,
            long_exchange.value,
            short_exchange.value,
            **kwargs,
        )

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
            result["ticker"] = await asyncio.wait_for(client.get_ticker(symbol), timeout=8.0)
        except Exception as exc:
            logger.debug(f"Could not get ticker from {exchange.value}: {exc}")

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

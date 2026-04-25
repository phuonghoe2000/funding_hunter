from __future__ import annotations

import asyncio
import logging
import statistics
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from math import inf
from typing import Any, Dict, Optional

from config.constants import (
    CASH_CARRY_SUPPORTED_EXCHANGES,
    Exchange,
    PositionStatus,
    Side,
    get_exchange_fee,
    get_exchange_symbol,
    get_spot_exchange_fee,
)
from config.settings import Settings
from core.session_store import SessionStore
from core.trade_advisor import (
    build_liquidation_context,
    build_liquidation_reduce_policy,
    estimate_dynamic_slippage_pct,
    estimate_order_book_depth_usd,
)
from exchanges.aster_client import AsterClient
from exchanges.aster_spot_client import AsterSpotClient
from exchanges.base import Position
from exchanges.binance_client import BinanceClient
from exchanges.binance_spot_client import BinanceSpotClient
from exchanges.spot_base import BaseSpotExchangeClient, SpotBalance

logger = logging.getLogger(__name__)

DEFAULT_CARRY_TARGET_HOURS = 4.0
DEFAULT_CARRY_MAX_DEPTH_SHARE_PCT = 0.25
DEFAULT_CARRY_MAX_MARGIN_USAGE_PCT = 0.85


@dataclass
class CarryTradePlan:
    pair: str
    strategy_type: str
    spot_exchange: Exchange
    future_exchange: Exchange
    leverage: int
    requested_size_tokens: float
    requested_notional_usd: float
    recommended_size_tokens: float
    recommended_notional_usd: float
    max_notional_usd: float
    spot_balance_cap_usd: Optional[float]
    future_balance_cap_usd: Optional[float]
    depth_cap_usd: Optional[float]
    spot_price: float
    future_price: float
    entry_basis_pct: float
    exit_basis_pct: float
    funding_edge_pct: float
    round_trip_cost_pct: float
    net_carry_edge_pct: float
    expected_funding_pnl_next_cycle_usd: float
    expected_edge_pnl_next_cycle_usd: float
    spot_depth_usd: float
    future_depth_usd: float
    available_depth_usd: float
    depth_ratio: float
    estimated_slippage_pct: float
    quality_score: float
    recommendation: str
    can_open: bool
    funding_interval_hours: int
    funding_window_hours: Optional[float]
    warnings: list[str]
    blockers: list[str]

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["spot_exchange"] = self.spot_exchange.value
        payload["future_exchange"] = self.future_exchange.value
        return payload


def _resolve_supported_exchange(name: str) -> Exchange:
    for exchange in CASH_CARRY_SUPPORTED_EXCHANGES:
        if exchange.value.lower() == name.lower():
            return exchange
    raise ValueError(f"Unsupported cash-carry exchange: {name}")


def _base_asset(pair: str) -> str:
    return pair.split("/")[0]


def _first_price(levels: Any) -> Optional[float]:
    if not levels:
        return None
    return float(levels[0][0])


def _hours_until(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        delta = value - datetime.now(timezone.utc)
    except Exception:
        return None
    return max(0.0, delta.total_seconds() / 3600)


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


class CashCarryEngine:
    def __init__(
        self,
        settings: Settings,
        *,
        session_store: Optional[SessionStore] = None,
        futures_manager: Any = None,
    ):
        self.settings = settings
        self.session_store = session_store or SessionStore()
        self.futures_manager = futures_manager
        self.spot_clients: dict[Exchange, BaseSpotExchangeClient] = {}

    def attach_future_manager(self, manager: Any) -> None:
        self.futures_manager = manager

    async def connect_enabled_spot_clients(
        self,
        *,
        enabled_exchanges: Optional[set[Exchange]] = None,
        debug: bool = False,
    ) -> list[str]:
        connected: list[str] = []
        specs = [
            (Exchange.BINANCE, self.settings.binance, BinanceSpotClient),
            (Exchange.ASTERDEX, self.settings.asterdex, AsterSpotClient),
        ]
        for exchange, config, client_cls in specs:
            if enabled_exchanges is not None and exchange not in enabled_exchanges:
                continue
            if not getattr(config, "api_key", ""):
                continue
            if exchange in self.spot_clients:
                connected.append(exchange.value)
                continue
            client = client_cls(config, debug=debug)
            if await client.connect():
                self.spot_clients[exchange] = client
                connected.append(exchange.value)
        return connected

    async def disconnect_all(self) -> None:
        for exchange, client in list(self.spot_clients.items()):
            try:
                await client.disconnect()
            except Exception as exc:
                logger.error("Error disconnecting %s spot client: %s", exchange.value, exc)
        self.spot_clients.clear()

    def _get_spot_client(self, exchange: Exchange) -> BaseSpotExchangeClient:
        client = self.spot_clients.get(exchange)
        if not client:
            raise RuntimeError(f"{exchange.value} spot client is not connected")
        return client

    def _get_future_client(self, exchange: Exchange) -> Any:
        if not self.futures_manager:
            raise RuntimeError("Futures manager is not attached")
        client = self.futures_manager.clients.get(exchange)
        if not client:
            raise RuntimeError(f"{exchange.value} futures client is not connected")
        return client

    async def _spot_balances_for_pair(self, pair: str, exchange: Exchange) -> dict[str, SpotBalance]:
        client = self._get_spot_client(exchange)
        base = _base_asset(pair)
        usdt_balance, base_balance = await asyncio.gather(
            client.get_balance("USDT"),
            client.get_balance(base),
        )
        return {"quote": usdt_balance, "base": base_balance}

    async def _future_context_for_pair(self, pair: str, exchange: Exchange) -> dict[str, Any]:
        client = self._get_future_client(exchange)
        symbol = get_exchange_symbol(pair, exchange)
        balance, funding, ticker, order_book, mark_price, position = await asyncio.gather(
            client.get_balance(),
            client.get_funding_rate(symbol),
            client.get_ticker(symbol),
            client.get_order_book(symbol, limit=10),
            client.get_mark_price(symbol),
            client.get_position(symbol),
        )
        return {
            "symbol": symbol,
            "balance": balance,
            "funding": funding,
            "ticker": ticker,
            "order_book": order_book,
            "mark_price": mark_price,
            "position": position,
        }

    async def get_pair_snapshot(
        self,
        pair: str,
        spot_exchange: Exchange,
        future_exchange: Exchange,
    ) -> Dict[str, Any]:
        spot_client = self._get_spot_client(spot_exchange)
        future_client = self._get_future_client(future_exchange)
        spot_symbol = get_exchange_symbol(pair, spot_exchange)
        future_symbol = get_exchange_symbol(pair, future_exchange)

        spot_order_book, spot_ticker, spot_price, spot_balances = await asyncio.gather(
            spot_client.get_order_book(spot_symbol, limit=10),
            spot_client.get_ticker(spot_symbol),
            spot_client.get_mark_price(spot_symbol),
            self._spot_balances_for_pair(pair, spot_exchange),
        )
        future_context = await self._future_context_for_pair(pair, future_exchange)

        spot_ask = _first_price(spot_order_book.get("asks"))
        spot_bid = _first_price(spot_order_book.get("bids"))
        future_bid = _first_price(future_context["order_book"].get("bids"))
        future_ask = _first_price(future_context["order_book"].get("asks"))

        open_basis_pct = None
        if spot_ask and future_bid:
            avg_price = (spot_ask + future_bid) / 2
            open_basis_pct = ((future_bid - spot_ask) / avg_price) * 100

        close_basis_pct = None
        if spot_bid and future_ask:
            avg_close = (spot_bid + future_ask) / 2
            close_basis_pct = ((spot_bid - future_ask) / avg_close) * 100

        next_funding_time = getattr(future_context["funding"], "next_funding_time", None)
        return {
            "strategy_type": "cash_carry",
            "pair": pair,
            "spot_exchange": spot_exchange,
            "future_exchange": future_exchange,
            "spot_symbol": spot_symbol,
            "future_symbol": future_symbol,
            "spot": {
                "order_book": spot_order_book,
                "ticker": spot_ticker,
                "price": spot_price,
                "balances": spot_balances,
            },
            "future": future_context,
            "spot_entry_price": spot_ask or spot_price,
            "future_entry_price": future_bid or future_context["mark_price"],
            "spot_exit_price": spot_bid or spot_price,
            "future_exit_price": future_ask or future_context["mark_price"],
            "open_basis_pct": open_basis_pct,
            "close_basis_pct": close_basis_pct,
            "next_funding_time": next_funding_time,
            "hours_until_funding": _hours_until(next_funding_time),
            "time_until_funding_text": (
                f"{int(max(0, (next_funding_time - datetime.now(timezone.utc)).total_seconds()) // 3600):02d}:"
                f"{int((max(0, (next_funding_time - datetime.now(timezone.utc)).total_seconds()) % 3600) // 60):02d}:"
                f"{int(max(0, (next_funding_time - datetime.now(timezone.utc)).total_seconds()) % 60):02d}"
                if next_funding_time
                else "N/A"
            ),
        }

    def build_trade_plan_from_snapshot(
        self,
        snapshot: Dict[str, Any],
        *,
        requested_size_tokens: float,
        leverage: int,
    ) -> CarryTradePlan:
        spot_exchange = snapshot["spot_exchange"]
        future_exchange = snapshot["future_exchange"]
        spot_data = snapshot["spot"]
        future_data = snapshot["future"]

        spot_price = _safe_float(snapshot.get("spot_entry_price"))
        future_price = _safe_float(snapshot.get("future_entry_price"))
        avg_entry_price = (spot_price + future_price) / 2 if spot_price and future_price else 0.0
        requested_notional_usd = requested_size_tokens * avg_entry_price if avg_entry_price > 0 else 0.0

        spot_depth_usd = estimate_order_book_depth_usd(spot_data.get("order_book"), "asks")
        future_depth_usd = estimate_order_book_depth_usd(future_data.get("order_book"), "bids")
        available_depth_usd = min(spot_depth_usd, future_depth_usd) if spot_depth_usd > 0 and future_depth_usd > 0 else 0.0
        depth_ratio = available_depth_usd / requested_notional_usd if requested_notional_usd > 0 else inf
        estimated_slippage_pct = estimate_dynamic_slippage_pct(depth_ratio if depth_ratio != inf else 10.0)

        funding_obj = future_data["funding"]
        funding_interval_hours = getattr(funding_obj, "funding_interval_hours", 8) or 8
        funding_edge_pct = float(getattr(funding_obj, "funding_rate", 0.0)) * 100 * (DEFAULT_CARRY_TARGET_HOURS / funding_interval_hours)

        entry_basis_pct = _safe_float(snapshot.get("open_basis_pct"))
        exit_basis_pct = _safe_float(snapshot.get("close_basis_pct"))
        round_trip_cost_pct = (
            (get_spot_exchange_fee(spot_exchange) + get_exchange_fee(future_exchange)) * 2
            + (estimated_slippage_pct * 4)
        )
        net_carry_edge_pct = entry_basis_pct + funding_edge_pct - round_trip_cost_pct

        spot_quote_balance = spot_data["balances"]["quote"]
        future_balance = future_data["balance"]
        spot_balance_cap_usd = _safe_float(spot_quote_balance.available) * 0.95
        future_balance_cap_usd = _safe_float(future_balance.available) * leverage * DEFAULT_CARRY_MAX_MARGIN_USAGE_PCT
        depth_cap_usd = available_depth_usd * DEFAULT_CARRY_MAX_DEPTH_SHARE_PCT if available_depth_usd > 0 else None

        caps = [value for value in [spot_balance_cap_usd, future_balance_cap_usd, depth_cap_usd] if value is not None and value > 0]
        max_notional_usd = min(caps) if caps else requested_notional_usd
        if max_notional_usd <= 0:
            max_notional_usd = requested_notional_usd
        recommended_notional_usd = min(requested_notional_usd, max_notional_usd) if requested_notional_usd > 0 else max_notional_usd
        recommended_size_tokens = recommended_notional_usd / avg_entry_price if avg_entry_price > 0 else 0.0
        expected_funding_pnl_next_cycle_usd = recommended_notional_usd * (funding_edge_pct / 100)
        expected_edge_pnl_next_cycle_usd = recommended_notional_usd * (net_carry_edge_pct / 100)

        warnings: list[str] = []
        blockers: list[str] = []

        if funding_edge_pct <= 0:
            warnings.append("Funding does not currently favor the short futures leg.")
        if entry_basis_pct <= 0:
            warnings.append("Executable basis is not positive at entry.")
        if net_carry_edge_pct <= 0:
            blockers.append("Combined carry edge is negative after fees and slippage.")
        elif net_carry_edge_pct < 0.05:
            warnings.append("Combined carry edge is thin after costs.")

        if requested_notional_usd > 0 and depth_ratio < 1:
            blockers.append("Requested size exceeds visible spot/futures top-of-book depth.")
        elif requested_notional_usd > 0 and depth_ratio < 2:
            warnings.append(f"Visible depth is only {depth_ratio:.2f}x the requested size.")

        if requested_notional_usd > spot_balance_cap_usd > 0:
            blockers.append(f"Spot USDT balance only supports about ${spot_balance_cap_usd:,.2f}.")
        if requested_notional_usd > future_balance_cap_usd > 0:
            warnings.append(f"Futures balance only supports about ${future_balance_cap_usd:,.2f} conservatively.")

        funding_window_hours = snapshot.get("hours_until_funding")
        if funding_window_hours is not None:
            if funding_window_hours > 6:
                warnings.append(f"Next funding is still {funding_window_hours:.1f}h away.")
            elif funding_window_hours <= 0.5:
                warnings.append("Funding window is close. Entry execution risk is higher.")

        score = 0.0
        if entry_basis_pct > 0:
            score += min(35.0, entry_basis_pct * 40)
        if funding_edge_pct > 0:
            score += min(20.0, funding_edge_pct * 200)
        if depth_ratio == inf:
            score += 25.0
        elif depth_ratio >= 10:
            score += 25.0
        elif depth_ratio >= 5:
            score += 20.0
        elif depth_ratio >= 3:
            score += 15.0
        elif depth_ratio >= 2:
            score += 10.0
        elif depth_ratio >= 1:
            score += 5.0
        if funding_window_hours is None:
            score += 5.0
        elif funding_window_hours <= 1:
            score += 10.0
        elif funding_window_hours <= 2:
            score += 8.0
        else:
            score += 4.0
        score = round(min(100.0, score), 1)

        if blockers:
            recommendation = "AVOID"
            can_open = False
        elif score >= 75:
            recommendation = "OPEN"
            can_open = True
        elif score >= 55:
            recommendation = "REDUCE"
            can_open = True
        elif score >= 35:
            recommendation = "WAIT"
            can_open = False
        else:
            recommendation = "AVOID"
            can_open = False

        return CarryTradePlan(
            pair=snapshot["pair"],
            strategy_type="cash_carry",
            spot_exchange=spot_exchange,
            future_exchange=future_exchange,
            leverage=leverage,
            requested_size_tokens=requested_size_tokens,
            requested_notional_usd=requested_notional_usd,
            recommended_size_tokens=recommended_size_tokens,
            recommended_notional_usd=recommended_notional_usd,
            max_notional_usd=max_notional_usd,
            spot_balance_cap_usd=spot_balance_cap_usd,
            future_balance_cap_usd=future_balance_cap_usd,
            depth_cap_usd=depth_cap_usd,
            spot_price=spot_price,
            future_price=future_price,
            entry_basis_pct=entry_basis_pct,
            exit_basis_pct=exit_basis_pct,
            funding_edge_pct=funding_edge_pct,
            round_trip_cost_pct=round_trip_cost_pct,
            net_carry_edge_pct=net_carry_edge_pct,
            expected_funding_pnl_next_cycle_usd=expected_funding_pnl_next_cycle_usd,
            expected_edge_pnl_next_cycle_usd=expected_edge_pnl_next_cycle_usd,
            spot_depth_usd=spot_depth_usd,
            future_depth_usd=future_depth_usd,
            available_depth_usd=available_depth_usd,
            depth_ratio=depth_ratio,
            estimated_slippage_pct=estimated_slippage_pct,
            quality_score=score,
            recommendation=recommendation,
            can_open=can_open,
            funding_interval_hours=funding_interval_hours,
            funding_window_hours=funding_window_hours,
            warnings=warnings,
            blockers=blockers,
        )

    async def build_open_assessment(
        self,
        *,
        pair: str,
        spot_exchange: Exchange,
        future_exchange: Exchange,
        requested_size_tokens: float,
        leverage: int,
    ) -> Dict[str, Any]:
        snapshot = await self.get_pair_snapshot(pair, spot_exchange, future_exchange)
        trade_plan = self.build_trade_plan_from_snapshot(
            snapshot,
            requested_size_tokens=requested_size_tokens,
            leverage=leverage,
        )
        snapshot["trade_plan"] = trade_plan.to_dict()
        return {"snapshot": snapshot, "trade_plan": trade_plan.to_dict()}

    async def preflight_open_execution(
        self,
        *,
        pair: str,
        spot_exchange: Exchange,
        future_exchange: Exchange,
        requested_size_tokens: float,
        leverage: int,
    ) -> Dict[str, Any]:
        blockers: list[str] = []
        warnings: list[str] = []
        details: Dict[str, Any] = {
            "pair": pair,
            "spot_exchange": spot_exchange,
            "future_exchange": future_exchange,
            "requested_size_tokens": requested_size_tokens,
            "leverage": leverage,
        }

        if requested_size_tokens <= 0:
            blockers.append("Requested size must be greater than zero.")

        try:
            spread_snapshot = await self.check_open_basis(pair, spot_exchange, future_exchange)
            details["spread_snapshot"] = spread_snapshot
        except Exception as exc:
            blockers.append(f"Market data preflight failed: {exc}")

        try:
            spot_balances = await self._spot_balances_for_pair(pair, spot_exchange)
            future_context = await self._future_context_for_pair(pair, future_exchange)
            details["spot_balances"] = spot_balances
            details["future_balance"] = future_context["balance"]

            notional_estimate = requested_size_tokens * _safe_float(details.get("spread_snapshot", {}).get("spot_ask_price"))
            if spot_balances["quote"].available <= 0:
                blockers.append(f"{spot_exchange.value} spot USDT balance is empty.")
            elif notional_estimate > 0 and spot_balances["quote"].available < notional_estimate * 0.98:
                blockers.append(
                    f"{spot_exchange.value} spot USDT balance ${spot_balances['quote'].available:,.2f} is below estimated notional ${notional_estimate:,.2f}."
                )

            future_position = future_context.get("position")
            if future_position and getattr(future_position, "size", 0.0):
                blockers.append(f"{future_exchange.value} already has an open futures position for {pair}.")

            base_balance = spot_balances["base"]
            if base_balance.total > 0:
                warnings.append(
                    f"{spot_exchange.value} spot already holds {base_balance.total:.6f} {_base_asset(pair)}. New carry will add on top."
                )
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
        spot_exchange: Exchange,
        future_exchange: Exchange,
        close_size: Optional[float] = None,
    ) -> Dict[str, Any]:
        blockers: list[str] = []
        warnings: list[str] = []
        details: Dict[str, Any] = {
            "pair": pair,
            "spot_exchange": spot_exchange,
            "future_exchange": future_exchange,
            "close_size": close_size,
        }

        try:
            current_state = await self._load_live_position_state(pair, spot_exchange, future_exchange)
            details["current_state"] = current_state
            closeable_size = current_state["closeable_size"]
            if closeable_size <= 0:
                blockers.append("No balanced spot/futures inventory found to close.")
            if close_size is not None and close_size > closeable_size:
                warnings.append(
                    f"Requested close size {close_size:.6f} exceeds current balanced size {closeable_size:.6f}; close will be capped."
                )

            spread_snapshot = await self.check_close_basis(pair, spot_exchange, future_exchange)
            details["spread_snapshot"] = spread_snapshot
        except Exception as exc:
            blockers.append(f"Close preflight failed: {exc}")

        return {
            "ready": not blockers,
            "blockers": blockers,
            "warnings": warnings,
            "details": details,
        }

    async def check_open_basis(
        self,
        pair: str,
        spot_exchange: Exchange,
        future_exchange: Exchange,
    ) -> Dict[str, float]:
        spot_client = self._get_spot_client(spot_exchange)
        future_client = self._get_future_client(future_exchange)
        spot_symbol = get_exchange_symbol(pair, spot_exchange)
        future_symbol = get_exchange_symbol(pair, future_exchange)
        spot_book, future_book = await asyncio.wait_for(
            asyncio.gather(
                spot_client.get_order_book(spot_symbol, limit=10),
                future_client.get_order_book(future_symbol, limit=10),
            ),
            timeout=10.0,
        )
        spot_ask = _first_price(spot_book.get("asks"))
        future_bid = _first_price(future_book.get("bids"))
        if spot_ask is None or future_bid is None:
            raise RuntimeError("Order book is missing spot ask or futures bid.")
        avg = (spot_ask + future_bid) / 2
        return {
            "spot_ask_price": spot_ask,
            "future_bid_price": future_bid,
            "entry_basis_pct": ((future_bid - spot_ask) / avg) * 100,
        }

    async def check_close_basis(
        self,
        pair: str,
        spot_exchange: Exchange,
        future_exchange: Exchange,
    ) -> Dict[str, float]:
        spot_client = self._get_spot_client(spot_exchange)
        future_client = self._get_future_client(future_exchange)
        spot_symbol = get_exchange_symbol(pair, spot_exchange)
        future_symbol = get_exchange_symbol(pair, future_exchange)
        spot_book, future_book = await asyncio.wait_for(
            asyncio.gather(
                spot_client.get_order_book(spot_symbol, limit=10),
                future_client.get_order_book(future_symbol, limit=10),
            ),
            timeout=10.0,
        )
        spot_bid = _first_price(spot_book.get("bids"))
        future_ask = _first_price(future_book.get("asks"))
        if spot_bid is None or future_ask is None:
            raise RuntimeError("Order book is missing spot bid or futures ask.")
        avg = (spot_bid + future_ask) / 2
        return {
            "spot_bid_price": spot_bid,
            "future_ask_price": future_ask,
            "exit_basis_pct": ((spot_bid - future_ask) / avg) * 100,
        }

    async def analyze_basis(
        self,
        *,
        pair: str,
        spot_exchange: Exchange,
        future_exchange: Exchange,
        duration_seconds: float,
        check_interval: float,
        mode: str,
        log_callback=None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "success": False,
            "samples": 0,
            "basis_values": [],
            "best_basis": None,
            "second_best_basis": None,
            "avg_basis": None,
            "error": None,
            "mode": mode,
        }

        def log_msg(message: str) -> None:
            if log_callback:
                log_callback(message)
            else:
                logger.info(message)

        getter = self.check_open_basis if mode == "open" else self.check_close_basis
        end_time = asyncio.get_running_loop().time() + duration_seconds
        basis_values: list[float] = []

        while asyncio.get_running_loop().time() < end_time:
            if cancel_event is not None and cancel_event.is_set():
                result["error"] = "Cancelled by user"
                return result
            try:
                snapshot = await getter(pair, spot_exchange, future_exchange)
                basis_pct = snapshot["entry_basis_pct"] if mode == "open" else snapshot["exit_basis_pct"]
                basis_values.append(basis_pct)
                result["samples"] = len(basis_values)
                result["basis_values"] = basis_values
                log_msg(f"Basis sample {len(basis_values)}: {basis_pct:.4f}%")
            except asyncio.TimeoutError:
                log_msg("Timeout fetching order books during carry analyze")
            except Exception as exc:
                log_msg(f"Error during carry analyze: {exc}")
            await asyncio.sleep(check_interval)

        if len(basis_values) < 2:
            result["error"] = f"Not enough samples ({len(basis_values)})"
            return result

        sorted_values = sorted(basis_values, reverse=True)
        result["success"] = True
        result["best_basis"] = sorted_values[0]
        result["second_best_basis"] = sorted_values[1]
        result["avg_basis"] = statistics.fmean(basis_values)
        result["median_basis"] = statistics.median(basis_values)
        result["min_basis"] = min(basis_values)
        result["max_basis"] = max(basis_values)
        return result

    async def _load_live_position_state(
        self,
        pair: str,
        spot_exchange: Exchange,
        future_exchange: Exchange,
    ) -> Dict[str, Any]:
        spot_client = self._get_spot_client(spot_exchange)
        future_client = self._get_future_client(future_exchange)
        spot_symbol = get_exchange_symbol(pair, spot_exchange)
        future_symbol = get_exchange_symbol(pair, future_exchange)
        base_asset = _base_asset(pair)

        spot_balance, future_position, spot_price = await asyncio.gather(
            spot_client.get_balance(base_asset),
            future_client.get_position(future_symbol),
            spot_client.get_mark_price(spot_symbol),
        )

        future_size = _safe_float(getattr(future_position, "size", 0.0))
        spot_size = _safe_float(spot_balance.total)
        closeable_size = min([value for value in [spot_size, future_size] if value > 0], default=0.0)
        return {
            "spot_size": spot_size,
            "future_size": future_size,
            "closeable_size": closeable_size,
            "spot_balance": spot_balance,
            "future_position": future_position,
            "spot_price": spot_price,
        }

    async def execute_open_splits(
        self,
        *,
        pair: str,
        spot_exchange: Exchange,
        future_exchange: Exchange,
        size: float,
        leverage: int,
        split_count: int,
        basis_threshold_pct: float,
        log_callback=None,
        cancel_event: Optional[threading.Event] = None,
        skip_leverage_set: bool = False,
        skip_basis_check: bool = False,
        split_interval_seconds: float = 2.0,
        max_wait_per_split: float = 3600.0,
        spread_check_interval: float = 2.0,
    ) -> Dict[str, Any]:
        def log_msg(message: str) -> None:
            if log_callback:
                log_callback(message)
            else:
                logger.info(message)

        result: Dict[str, Any] = {
            "success": False,
            "strategy_type": "cash_carry",
            "splits_completed": 0,
            "splits_total": split_count,
            "total_spot_size": 0.0,
            "total_future_size": 0.0,
            "error": None,
            "split_results": [],
            "cancelled": False,
        }

        spot_client = self._get_spot_client(spot_exchange)
        future_client = self._get_future_client(future_exchange)
        future_symbol = get_exchange_symbol(pair, future_exchange)

        if not skip_leverage_set:
            try:
                await future_client.set_leverage(future_symbol, leverage)
            except Exception as exc:
                result["error"] = f"Failed to set leverage on {future_exchange.value}: {exc}"
                return result

        size_per_split = size / max(1, split_count)
        current_threshold = basis_threshold_pct

        for index in range(split_count):
            split_num = index + 1
            if cancel_event is not None and cancel_event.is_set():
                result["cancelled"] = True
                result["error"] = "Cancelled by user"
                result["success"] = result["splits_completed"] > 0
                return result

            if index > 0 and not skip_basis_check:
                check_count = 0
                wait_start = asyncio.get_running_loop().time()
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        result["cancelled"] = True
                        result["error"] = "Cancelled by user"
                        result["success"] = result["splits_completed"] > 0
                        return result

                    basis_snapshot = await self.check_open_basis(pair, spot_exchange, future_exchange)
                    current_basis = basis_snapshot["entry_basis_pct"]
                    if current_basis >= current_threshold:
                        log_msg(f"Split {split_num}: entry basis {current_basis:.4f}% >= {current_threshold:.4f}%")
                        break

                    check_count += 1
                    if check_count >= 30:
                        old_threshold = current_threshold
                        current_threshold -= 0.01
                        check_count = 0
                        log_msg(f"Split {split_num}: reducing basis threshold {old_threshold:.4f}% -> {current_threshold:.4f}%")

                    elapsed = asyncio.get_running_loop().time() - wait_start
                    if max_wait_per_split > 0 and elapsed >= max_wait_per_split:
                        result["error"] = f"Timeout waiting for entry basis on split {split_num}"
                        result["success"] = result["splits_completed"] > 0
                        return result
                    await asyncio.sleep(spread_check_interval)

            try:
                spot_symbol = get_exchange_symbol(pair, spot_exchange)
                spot_order = await spot_client.place_market_order(spot_symbol, "BUY", size_per_split)
                bought_size = _safe_float(spot_order.filled_size or spot_order.size)
                if bought_size <= 0:
                    raise Exception("Spot buy did not fill any quantity")

                future_order = await future_client.place_market_order(future_symbol, Side.SHORT, bought_size)
                future_size = _safe_float(future_order.filled_size or future_order.size)
                balanced_size = min(value for value in [bought_size, future_size] if value > 0)
                result["split_results"].append(
                    {
                        "split": split_num,
                        "spot_order": spot_order.raw_data,
                        "future_order": future_order.raw_data,
                        "spot_size": bought_size,
                        "future_size": future_size,
                        "balanced_size": balanced_size,
                    }
                )
                result["splits_completed"] += 1
                result["total_spot_size"] += bought_size
                result["total_future_size"] += future_size
                log_msg(f"Split {split_num}/{split_count} opened: spot {bought_size:.6f}, future {future_size:.6f}")
            except Exception as exc:
                error_message = f"Split {split_num} failed: {exc}"
                result["error"] = error_message
                log_msg(error_message)
                try:
                    if "spot_order" in locals() and spot_order:
                        rollback_size = _safe_float(spot_order.filled_size or spot_order.size)
                        if rollback_size > 0:
                            await spot_client.place_market_order(get_exchange_symbol(pair, spot_exchange), "SELL", rollback_size)
                            log_msg(f"Rolled back spot buy for split {split_num}: sold {rollback_size:.6f}")
                except Exception as rollback_exc:
                    result["rollback_error"] = str(rollback_exc)
                    log_msg(f"Rollback failed for split {split_num}: {rollback_exc}")
                result["success"] = result["splits_completed"] > 0
                return result

            if split_num < split_count and split_interval_seconds > 0:
                await asyncio.sleep(split_interval_seconds)

        result["success"] = result["splits_completed"] == split_count
        result["total_size"] = min(result["total_spot_size"], result["total_future_size"])
        return result

    async def get_initial_position_state(
        self,
        *,
        pair: str,
        spot_exchange: Exchange,
        future_exchange: Exchange,
        size: float,
        leverage: int,
        pretrade_assessment: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        open_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        snapshot = await self.get_pair_snapshot(pair, spot_exchange, future_exchange)
        future_rate_obj = snapshot["future"]["funding"]
        state = {
            "strategy_type": "cash_carry",
            "pair": pair,
            "size": size,
            "spot_size": size,
            "future_size": size,
            "leverage": leverage,
            "spot_exchange": spot_exchange,
            "future_exchange": future_exchange,
            "long_exchange": spot_exchange,
            "short_exchange": future_exchange,
            "open_time": open_time or datetime.now(timezone.utc),
            "session_id": session_id or self.session_store.new_session_id(),
            "initial_spot_price": snapshot.get("spot_entry_price", 0.0),
            "initial_future_price": snapshot.get("future_entry_price", 0.0),
            "initial_long_rate": 0.0,
            "initial_short_rate": getattr(future_rate_obj, "funding_rate", 0.0),
            "initial_net_funding": getattr(future_rate_obj, "funding_rate", 0.0),
            "last_funding_check": datetime.now(timezone.utc),
            "funding_interval_hours": getattr(future_rate_obj, "funding_interval_hours", 8) or 8,
        }
        if pretrade_assessment:
            state["pretrade_assessment"] = pretrade_assessment
            state["initial_net_edge_pct"] = pretrade_assessment.get("net_carry_edge_pct", 0.0)
            state["initial_quality_score"] = pretrade_assessment.get("quality_score", 0.0)
        return state

    async def close_position_with_analysis(
        self,
        *,
        pair: str,
        spot_exchange: Exchange,
        future_exchange: Exchange,
        splits: int,
        log_callback=None,
        cancel_event: Optional[threading.Event] = None,
        skip_basis_check: bool = False,
        close_size: Optional[float] = None,
        split_interval_seconds: float = 2.0,
    ) -> Dict[str, Any]:
        preflight_result = await self.preflight_close_execution(
            pair=pair,
            spot_exchange=spot_exchange,
            future_exchange=future_exchange,
            close_size=close_size,
        )
        if not preflight_result["ready"]:
            return {
                "success": False,
                "error": " | ".join(preflight_result["blockers"]) or "Close preflight failed",
                "preflight_result": preflight_result,
                "fully_closed": False,
            }

        threshold = -100.0
        analyze_result = None
        if not skip_basis_check:
            analyze_result = await self.analyze_basis(
                pair=pair,
                spot_exchange=spot_exchange,
                future_exchange=future_exchange,
                duration_seconds=120.0,
                check_interval=2.0,
                mode="close",
                log_callback=log_callback,
                cancel_event=cancel_event,
            )
            if analyze_result.get("success"):
                threshold = analyze_result["second_best_basis"]

        result = await self._execute_close_splits(
            pair=pair,
            spot_exchange=spot_exchange,
            future_exchange=future_exchange,
            splits=splits,
            basis_threshold_pct=threshold,
            log_callback=log_callback,
            cancel_event=cancel_event,
            skip_basis_check=skip_basis_check,
            close_size=close_size,
            split_interval_seconds=split_interval_seconds,
        )
        result["analyze_result"] = analyze_result
        result["preflight_result"] = preflight_result
        remaining_position_state = await self.refresh_position_state(
            {
                "strategy_type": "cash_carry",
                "pair": pair,
                "spot_exchange": spot_exchange,
                "future_exchange": future_exchange,
                "long_exchange": spot_exchange,
                "short_exchange": future_exchange,
            }
        )
        result["remaining_position_state"] = remaining_position_state
        result["fully_closed"] = not remaining_position_state.get("has_position", True)
        return result

    async def _execute_close_splits(
        self,
        *,
        pair: str,
        spot_exchange: Exchange,
        future_exchange: Exchange,
        splits: int,
        basis_threshold_pct: float,
        log_callback=None,
        cancel_event: Optional[threading.Event] = None,
        skip_basis_check: bool = False,
        close_size: Optional[float] = None,
        split_interval_seconds: float = 2.0,
        max_wait_per_split: float = 3600.0,
        spread_check_interval: float = 2.0,
    ) -> Dict[str, Any]:
        def log_msg(message: str) -> None:
            if log_callback:
                log_callback(message)
            else:
                logger.info(message)

        result: Dict[str, Any] = {
            "success": False,
            "strategy_type": "cash_carry",
            "closed_splits": 0,
            "splits_total": splits,
            "closed_spot_size": 0.0,
            "closed_future_size": 0.0,
            "error": None,
            "split_results": [],
            "cancelled": False,
        }

        live_state = await self._load_live_position_state(pair, spot_exchange, future_exchange)
        closeable_size = live_state["closeable_size"]
        if close_size is not None:
            closeable_size = min(closeable_size, close_size)
        if closeable_size <= 0:
            result["error"] = "No balanced spot/futures size available to close"
            return result

        size_per_split = closeable_size / max(1, splits)
        current_threshold = basis_threshold_pct
        spot_client = self._get_spot_client(spot_exchange)
        future_client = self._get_future_client(future_exchange)
        spot_symbol = get_exchange_symbol(pair, spot_exchange)
        future_symbol = get_exchange_symbol(pair, future_exchange)

        for index in range(splits):
            split_num = index + 1
            if cancel_event is not None and cancel_event.is_set():
                result["cancelled"] = True
                result["error"] = "Cancelled by user"
                result["success"] = result["closed_splits"] > 0
                return result

            if index > 0 and not skip_basis_check:
                check_count = 0
                wait_start = asyncio.get_running_loop().time()
                while True:
                    basis_snapshot = await self.check_close_basis(pair, spot_exchange, future_exchange)
                    current_basis = basis_snapshot["exit_basis_pct"]
                    if current_basis >= current_threshold:
                        log_msg(f"Split {split_num}: exit basis {current_basis:.4f}% >= {current_threshold:.4f}%")
                        break

                    check_count += 1
                    if check_count >= 30:
                        old_threshold = current_threshold
                        current_threshold -= 0.01
                        check_count = 0
                        log_msg(f"Split {split_num}: reducing close basis threshold {old_threshold:.4f}% -> {current_threshold:.4f}%")

                    elapsed = asyncio.get_running_loop().time() - wait_start
                    if max_wait_per_split > 0 and elapsed >= max_wait_per_split:
                        result["error"] = f"Timeout waiting for close basis on split {split_num}"
                        result["success"] = result["closed_splits"] > 0
                        return result
                    await asyncio.sleep(spread_check_interval)

            try:
                future_close_order = await future_client.close_position_partial(future_symbol, size_per_split)
                future_closed_size = _safe_float(future_close_order.filled_size or future_close_order.size)
                if future_closed_size <= 0:
                    raise Exception("Futures close did not fill any quantity")

                spot_sell_order = await spot_client.place_market_order(spot_symbol, "SELL", future_closed_size)
                spot_closed_size = _safe_float(spot_sell_order.filled_size or spot_sell_order.size)
                result["split_results"].append(
                    {
                        "split": split_num,
                        "future_close_order": future_close_order.raw_data,
                        "spot_sell_order": spot_sell_order.raw_data,
                        "future_closed_size": future_closed_size,
                        "spot_closed_size": spot_closed_size,
                    }
                )
                result["closed_splits"] += 1
                result["closed_future_size"] += future_closed_size
                result["closed_spot_size"] += spot_closed_size
                log_msg(f"Split {split_num}/{splits} closed: future {future_closed_size:.6f}, spot {spot_closed_size:.6f}")
            except Exception as exc:
                error_message = f"Split {split_num} close failed: {exc}"
                result["error"] = error_message
                log_msg(error_message)
                try:
                    if "future_close_order" in locals() and future_close_order:
                        reopen_size = _safe_float(future_close_order.filled_size or future_close_order.size)
                        if reopen_size > 0:
                            await future_client.place_market_order(future_symbol, Side.SHORT, reopen_size)
                            log_msg(f"Reopened futures short for split {split_num}: {reopen_size:.6f}")
                except Exception as rollback_exc:
                    result["rollback_error"] = str(rollback_exc)
                    log_msg(f"Failed to re-short futures after split {split_num} error: {rollback_exc}")
                result["success"] = result["closed_splits"] > 0
                return result

            if split_num < splits and split_interval_seconds > 0:
                await asyncio.sleep(split_interval_seconds)

        result["success"] = result["closed_splits"] == splits
        return result

    async def refresh_position_state(self, position: Dict[str, Any]) -> Dict[str, Any]:
        live_state = await self._load_live_position_state(
            position["pair"],
            position["spot_exchange"],
            position["future_exchange"],
        )
        refreshed = dict(position)
        refreshed["spot_size"] = live_state["spot_size"]
        refreshed["future_size"] = live_state["future_size"]
        refreshed["size"] = live_state["closeable_size"]
        refreshed["last_position_refresh"] = datetime.now(timezone.utc)
        return {
            "has_position": live_state["spot_size"] > 0 or live_state["future_size"] > 0,
            "position": refreshed,
            "check_result": {
                "spot_balance": live_state["spot_balance"],
                "future_position": live_state["future_position"],
            },
        }

    async def recover_active_session(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        restored = dict(payload)
        for field in ("spot_exchange", "future_exchange", "long_exchange", "short_exchange"):
            value = restored.get(field)
            if isinstance(value, str):
                restored[field] = Exchange(value)
        for field in ("open_time", "saved_at", "last_risk_reduce_at"):
            value = restored.get(field)
            if isinstance(value, str):
                try:
                    restored[field] = datetime.fromisoformat(value)
                except ValueError:
                    pass

        state = await self.refresh_position_state(restored)
        if not state["has_position"]:
            self.session_store.clear_active_session()
            self.session_store.append_journal_event(
                "session_stale",
                {"pair": restored.get("pair"), "strategy_type": "cash_carry"},
            )
            return None
        restored.update(state["position"])
        return restored

    async def build_monitor_snapshot(self, position: Dict[str, Any]) -> Dict[str, Any]:
        snapshot = await self.get_pair_snapshot(
            position["pair"],
            position["spot_exchange"],
            position["future_exchange"],
        )
        trade_plan = self.build_trade_plan_from_snapshot(
            snapshot,
            requested_size_tokens=float(position.get("size", 0.0) or 0.0),
            leverage=int(position.get("leverage", 1) or 1),
        )
        spot_balance = snapshot["spot"]["balances"]["base"]
        future_position = snapshot["future"]["position"]
        spot_mark_price = _safe_float(snapshot["spot"].get("price"))

        initial_spot_price = _safe_float(position.get("initial_spot_price"))
        spot_qty = _safe_float(spot_balance.total)
        spot_pnl = (spot_mark_price - initial_spot_price) * spot_qty if initial_spot_price > 0 else 0.0
        future_pnl = _safe_float(getattr(future_position, "unrealized_pnl", 0.0))

        spot_quote_balance = snapshot["spot"]["balances"]["quote"]
        future_balance = snapshot["future"]["balance"]
        total_balance = (
            _safe_float(spot_quote_balance.available)
            + (spot_qty * spot_mark_price)
            + _safe_float(future_balance.available)
        )
        if total_balance <= 0:
            total_balance = 1000.0

        risk_percent = ((abs(future_pnl) if future_pnl < 0 else 0.0) / total_balance) * 100

        one_side_missing = None
        if spot_qty <= 0 < _safe_float(getattr(future_position, "size", 0.0)):
            one_side_missing = position["spot_exchange"]
        elif _safe_float(getattr(future_position, "size", 0.0)) <= 0 < spot_qty:
            one_side_missing = position["future_exchange"]

        spot_position = Position(
            symbol=snapshot["spot_symbol"],
            side=Side.LONG,
            size=spot_qty,
            entry_price=initial_spot_price,
            mark_price=spot_mark_price,
            liquidation_price=0.0,
            unrealized_pnl=spot_pnl,
            leverage=1,
            status=PositionStatus.OPEN,
            timestamp=datetime.now(timezone.utc),
            raw_data={"asset": _base_asset(position["pair"])},
        ) if spot_qty > 0 else None

        check_result = {
            "long": spot_position,
            "short": future_position,
            "one_side_missing": one_side_missing,
        }
        liquidation = build_liquidation_context(position=position, check_result=check_result)
        liq_policy = build_liquidation_reduce_policy(
            distance_pct=liquidation.get("min_liquidation_distance_pct"),
            activation_threshold_pct=3.0,
        )

        advice_action = "HOLD"
        advice_reason = "Spot/futures carry remains balanced."
        if one_side_missing:
            advice_action = "EMERGENCY_CLOSE"
            advice_reason = f"One carry leg is missing on {one_side_missing.value}."
        elif trade_plan.net_carry_edge_pct <= 0:
            advice_action = "CLOSE_NOW"
            advice_reason = "Combined carry edge is now negative."
        elif liq_policy and liq_policy.reduce_ratio >= 1.0:
            advice_action = "EMERGENCY_CLOSE"
            advice_reason = liq_policy.description
        elif liq_policy:
            advice_action = "REDUCE"
            advice_reason = liq_policy.description

        return {
            "strategy_type": "cash_carry",
            "total_balance": total_balance,
            "balances": {
                position["spot_exchange"]: snapshot["spot"]["balances"]["quote"],
                position["future_exchange"]: future_balance,
            },
            "long_pnl": spot_pnl,
            "short_pnl": future_pnl,
            "risk_percent": risk_percent,
            "liquidation": liquidation,
            "liquidation_policy": liq_policy.to_dict() if liq_policy else None,
            "min_liquidation_distance_pct": liquidation.get("min_liquidation_distance_pct"),
            "check_result": check_result,
            "pair_snapshot": snapshot,
            "trade_plan": trade_plan.to_dict(),
            "monitor_advice": {
                "action": advice_action,
                "reason": advice_reason,
                "current_net_edge_pct": trade_plan.net_carry_edge_pct,
                "expected_next_cycle_pnl_usd": trade_plan.expected_edge_pnl_next_cycle_usd,
            },
        }

    async def reduce_position_on_risk(
        self,
        *,
        pair: str,
        spot_exchange: Exchange,
        future_exchange: Exchange,
        reduce_ratio: float,
        splits: int,
        interval_seconds: float,
        risk_percent: Optional[float] = None,
        threshold_percent: Optional[float] = None,
        reason: str = "risk_threshold",
        policy_mode: str = "risk_reduce",
        action_label: str = "REDUCE_50",
    ) -> Dict[str, Any]:
        live_state = await self._load_live_position_state(pair, spot_exchange, future_exchange)
        current_size = live_state["closeable_size"]
        if current_size <= 0:
            return {"success": False, "error": "No balanced size available to reduce"}

        close_size = current_size * reduce_ratio
        close_result = await self.close_position_with_analysis(
            pair=pair,
            spot_exchange=spot_exchange,
            future_exchange=future_exchange,
            splits=splits,
            skip_basis_check=True,
            close_size=close_size,
            split_interval_seconds=interval_seconds,
        )
        refreshed = close_result.get("remaining_position_state", {})
        position_state = refreshed.get("position", {})
        close_result.update(
            {
                "mode": "risk_reduce",
                "policy_mode": policy_mode,
                "action_label": action_label,
                "reduce_ratio": reduce_ratio,
                "requested_close_size": close_size,
                "risk_percent": risk_percent,
                "threshold_percent": threshold_percent,
                "remaining_long_size": position_state.get("spot_size", 0.0),
                "remaining_short_size": position_state.get("future_size", 0.0),
                "remaining_size_tokens": position_state.get("size", 0.0),
                "reason": reason,
            }
        )
        return close_result
